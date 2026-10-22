import os
import sys
import time
import sqlite3
import urllib.parse
import requests

from sync_nextcloud import (
    scan_nextcloud_docs,
    process_new_nextcloud_file,
    process_existing_nextcloud_file,
    process_deleted_nextcloud_files
)
from sync_paperless import (
    wait_for_paperless_task,
    build_tag_cache,
    process_new_paperless_document,
    process_existing_paperless_document,
    process_deleted_paperless_documents
)

PAPERLESS_URL = os.getenv("PAPERLESS_API_URL", "").rstrip("/")
PAPERLESS_TOKEN = os.getenv("PAPERLESS_API_TOKEN", "").strip()
NC_BASE_URL = os.getenv("NEXTCLOUD_BASE_URL", "").rstrip("/")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "10"))

def read_secret_or_env(file_env_key: str, val_env_key: str) -> str:
    file_path = os.getenv(file_env_key)
    if file_path and os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            return f.read().strip()
    return os.getenv(val_env_key, "").strip()

NC_USER = read_secret_or_env("NEXTCLOUD_USER_FILE", "NEXTCLOUD_USER")
NC_PASS = read_secret_or_env("NEXTCLOUD_PASS_FILE", "NEXTCLOUD_PASS")
NC_FOLDER = os.getenv("NEXTCLOUD_FOLDER", "Documents").strip("/")
NC_DOCS_DAV_URL = f"{NC_BASE_URL}/remote.php/dav/files/{urllib.parse.quote(NC_USER)}/{urllib.parse.quote(NC_FOLDER)}"

PL_HEADERS = {"Authorization": f"Token {PAPERLESS_TOKEN}"}
NC_AUTH = (NC_USER, NC_PASS)

# --- SQLITE STATE DATABASE ---
os.makedirs("/app/state", exist_ok=True)
db = sqlite3.connect("/app/state/sync.db", check_same_thread=False)
cursor = db.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS file_map (
        paperless_id      INTEGER PRIMARY KEY,
        nextcloud_file_id TEXT UNIQUE NOT NULL,
        nextcloud_path    TEXT NOT NULL,
        last_synced       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
""")
db.commit()
# --- SQLITE STATE DATABASE END ---

def sync_nextcloud_state():
    files = scan_nextcloud_docs(NC_DOCS_DAV_URL, NC_AUTH, NC_USER, NC_FOLDER)
    process_deleted_nextcloud_files(cursor, db, files, PAPERLESS_URL, PL_HEADERS, NC_DOCS_DAV_URL, NC_AUTH)

    for item in files:
        nc_id = item["file_id"]
        rel_path = item["rel_path"]
        folder_tag = item["folder"]
        filename = item["filename"]

        cursor.execute("SELECT paperless_id, nextcloud_path FROM file_map WHERE nextcloud_file_id = ?", (nc_id,))
        row = cursor.fetchone()

        if row is None:
            title = os.path.splitext(filename)[0]
            check_res = requests.get(
                f"{PAPERLESS_URL}/documents/",
                params={"title__iexact": title},
                headers=PL_HEADERS,
                timeout=10
            )

            # If a document with the same title already exists in Paperless, link it instead of creating a new one
            if check_res.ok and check_res.json().get("count", 0) > 0:
                paperless_doc_id = check_res.json()["results"][0]["id"]
                cursor.execute(
                    "INSERT INTO file_map (paperless_id, nextcloud_file_id, nextcloud_path) VALUES (?, ?, ?)",
                    (paperless_doc_id, nc_id, rel_path)
                )
                db.commit()
                print(f"[LINKED] Matched pre-existing Nextcloud file '{rel_path}' to Paperless ID #{paperless_doc_id}")
            else:
                process_new_nextcloud_file(
                    NC_DOCS_DAV_URL, NC_AUTH, PAPERLESS_URL, PL_HEADERS, 
                    cursor, db, nc_id, rel_path, folder_tag, filename, wait_for_paperless_task
                )
        else:
            paperless_id, stored_path = row
            # Update/rename handler
            process_existing_nextcloud_file(
                paperless_id, nc_id, stored_path, rel_path, folder_tag, filename, 
                PAPERLESS_URL, PL_HEADERS, cursor, db
            )

def sync_paperless_state():
    # Build a local cache of all tags to prevent N+1 API calls
    tags_cache = build_tag_cache(PAPERLESS_URL, PL_HEADERS)

    # Paginate all documents
    pl_docs = []
    next_page_url = f"{PAPERLESS_URL}/documents/"
    
    while next_page_url:
        res = requests.get(next_page_url, headers=PL_HEADERS, timeout=15)
        if not res.ok:
            return
        data = res.json()
        pl_docs.extend(data.get("results", []))
        next_page_url = data.get("next")

    # Deletions
    process_deleted_paperless_documents(cursor, db, pl_docs, NC_DOCS_DAV_URL, NC_AUTH)

    # Updates and New Files
    for doc in pl_docs:
        # PDF only
        original_name = doc.get("original_file_name", "")
        if not original_name.lower().endswith(".pdf"):
            continue

        paperless_id = doc["id"]
        title = doc["title"]
        tag_ids = doc.get("tags", [])
        
        folder_tag = tags_cache.get(tag_ids[0], "unlabeled") if tag_ids else "unlabeled"

        cursor.execute("SELECT nextcloud_file_id, nextcloud_path FROM file_map WHERE paperless_id = ?", (paperless_id,))
        row = cursor.fetchone()

        if row is None:
            # Check if Nextcloud already has a file at this exact path
            filename = f"{title}.pdf"
            rel_path = f"{folder_tag}/{filename}"
            
            from sync_nextcloud import get_nextcloud_file_id
            existing_nc_id = get_nextcloud_file_id(NC_DOCS_DAV_URL, NC_AUTH, rel_path)

            # If a file with the same path already exists in Nextcloud, link it instead of creating a new one
            if existing_nc_id:
                cursor.execute(
                    "INSERT INTO file_map (paperless_id, nextcloud_file_id, nextcloud_path) VALUES (?, ?, ?)",
                    (paperless_id, existing_nc_id, rel_path)
                )
                db.commit()
                print(f"[LINKED] Matched pre-existing Paperless ID #{paperless_id} to Nextcloud file '{rel_path}'")
            else:
                process_new_paperless_document(
                    paperless_id, title, folder_tag, PAPERLESS_URL, PL_HEADERS, 
                    NC_DOCS_DAV_URL, NC_AUTH, cursor, db
                )
        else:
            nc_id, stored_path = row
            process_existing_paperless_document(
                paperless_id, nc_id, stored_path, title, folder_tag, 
                NC_DOCS_DAV_URL, NC_AUTH, cursor, db
            )

def main():
    if not PAPERLESS_TOKEN:
        print("[ERROR] PAPERLESS_API_TOKEN is missing!")
        sys.exit(1)
    if not PAPERLESS_URL:
        print("[ERROR] PAPERLESS_API_URL is missing!")
        sys.exit(1)
    if not NC_BASE_URL:
        print("[ERROR] NEXTCLOUD_BASE_URL is missing!")
        sys.exit(1)

    print(f"Sync Bridge started. Watching Nextcloud WebDAV & Paperless API.")
    while True:
        try:
            sync_nextcloud_state()
            sync_paperless_state()
        except Exception as e:
            print(f"[ERROR] Sync cycle failed: {e}")
        time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()