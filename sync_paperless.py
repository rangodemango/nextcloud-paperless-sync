import os
import time
import requests
import urllib.parse

def get_or_create_tag(paperless_url: str, pl_headers: dict, tag_name: str) -> int:
    """Finds a tag by name in Paperless or creates it if it doesn't exist."""
    res = requests.get(
        f"{paperless_url}/tags/",
        params={"name__iexact": tag_name},
        headers=pl_headers,
        timeout=15
    )
    res.raise_for_status()
    data = res.json()
    if data.get("count", 0) > 0:
        return data["results"][0]["id"]

    create_res = requests.post(
        f"{paperless_url}/tags/",
        headers=pl_headers,
        json={"name": tag_name},
        timeout=15
    )
    create_res.raise_for_status()
    return create_res.json()["id"]

def build_tag_cache(paperless_url: str, pl_headers: dict) -> dict:
    """Fetches all tags from Paperless across all pages and builds an ID-to-Name dictionary."""
    tags_cache = {}
    next_page_url = f"{paperless_url}/tags/"
    
    while next_page_url:
        res = requests.get(next_page_url, headers=pl_headers, timeout=15)
        if not res.ok:
            print(f"[ERROR] Failed to fetch tags: {res.status_code}")
            break
            
        data = res.json()
        for tag in data.get("results", []):
            tags_cache[tag["id"]] = tag["name"]
            
        next_page_url = data.get("next")
        
    return tags_cache

def ensure_nextcloud_folder(base_url: str, auth: tuple, folder_path: str):
    """Recursively creates nested folders in Nextcloud using WebDAV MKCOL."""
    parts = folder_path.split("/")
    current_path = ""
    for part in parts:
        current_path = f"{current_path}/{part}" if current_path else part
        target_url = f"{base_url}/{urllib.parse.quote(current_path)}"
        requests.request("MKCOL", target_url, auth=auth, timeout=10)

def wait_for_paperless_task(paperless_url: str, pl_headers: dict, task_uuid: str) -> int | None:
    """Polls the Paperless task queue strictly by task_uuid, avoiding filename fallbacks."""
    for _ in range(30):  # 60 seconds total wait time
        res = requests.get(
            f"{paperless_url}/tasks/",
            params={"task_id": task_uuid},
            headers=pl_headers,
            timeout=10
        )
        
        if res.ok:
            data = res.json()
            tasks = data.get("results", []) if isinstance(data, dict) else data
            
            if tasks:
                task = tasks[0]
                status = task.get("status")
                
                if status == "success" or status == "SUCCESS":
                    doc_ids = task.get("related_document_ids", [])
                    
                    if doc_ids:
                        doc_id = int(doc_ids[0])
                        
                        requests.post(
                            f"{paperless_url}/tasks/acknowledge/",
                            headers=pl_headers,
                            json={"tasks": [task.get("id")]},
                            timeout=5
                        )
                        return doc_id
                    
                elif status == "failure" or status == "FAILURE":
                    print(f"[ERROR] Paperless task failed: {task.get('result')}")
                    requests.post(
                        f"{paperless_url}/tasks/acknowledge/",
                        headers=pl_headers,
                        json={"tasks": [task.get("id")]},
                        timeout=5
                    )
                    return None
                    
        time.sleep(2)

    print(f"[WARN] Timed out waiting for Paperless task {task_uuid}")
    return None

def process_new_paperless_document(paperless_id: int, title: str, folder_tag: str, paperless_url: str, pl_headers: dict, nc_docs_dav_url: str, nc_auth: tuple, cursor, db):
    """Downloads a document created in Paperless and pushes it to Nextcloud."""
    print(f"[PAPERLESS NEW] Found unregistered document in Paperless: #{paperless_id} ({title})")
    
    dl_res = requests.get(f"{paperless_url}/documents/{paperless_id}/download/", headers=pl_headers, timeout=30)
    if not dl_res.ok:
        print(f"[ERROR] Failed to download Paperless document #{paperless_id}")
        return
        
    filename = f"{title}.pdf"
    rel_path = f"{folder_tag}/{filename}"
    
    ensure_nextcloud_folder(nc_docs_dav_url, nc_auth, folder_tag)
    
    # Upload file
    put_res = requests.put(f"{nc_docs_dav_url}/{urllib.parse.quote(rel_path)}", auth=nc_auth, data=dl_res.content, timeout=30)
    put_res.raise_for_status()

    from sync_nextcloud import get_nextcloud_file_id
    nc_id = get_nextcloud_file_id(nc_docs_dav_url, nc_auth, rel_path)
    
    if nc_id:
        cursor.execute(
            "INSERT INTO file_map (paperless_id, nextcloud_file_id, nextcloud_path) VALUES (?, ?, ?)",
            (paperless_id, nc_id, rel_path)
        )
        db.commit()
        print(f"[PAPERLESS MAPPED] Paperless ID #{paperless_id} <-> Nextcloud ID {nc_id}")

def process_existing_paperless_document(paperless_id: int, nc_id: str, stored_path: str, title: str, folder_tag: str, nc_docs_dav_url: str, nc_auth: tuple, cursor, db):
    """Checks if a Paperless document was renamed or its tags changed, and updates Nextcloud accordingly."""
    filename = f"{title}.pdf"
    new_path = f"{folder_tag}/{filename}"

    if stored_path != new_path:
        print(f"[PAPERLESS UPDATE] Detected change for Paperless ID #{paperless_id}: '{stored_path}' -> '{new_path}'")

        ensure_nextcloud_folder(nc_docs_dav_url, nc_auth, folder_tag)

        old_url = f"{nc_docs_dav_url}/{urllib.parse.quote(stored_path)}"
        new_url = f"{nc_docs_dav_url}/{urllib.parse.quote(new_path)}"

        move_res = requests.request(
            "MOVE",
            old_url,
            auth=nc_auth,
            headers={"Destination": new_url},
            timeout=15
        )

        if move_res.ok or move_res.status_code == 201:
            cursor.execute(
                "UPDATE file_map SET nextcloud_path = ? WHERE paperless_id = ?",
                (new_path, paperless_id)
            )
            db.commit()
            print(f"[PAPERLESS SYNCED] Moved/Renamed Nextcloud file to '{new_path}'")
        else:
            print(f"[ERROR] Failed to move Nextcloud file: {move_res.text}")

def process_deleted_paperless_documents(cursor, db, active_docs: list, nc_docs_dav_url: str, nc_auth: tuple):
    """Checks SQLite for tracked documents removed from Paperless and deletes them from Nextcloud."""
    active_pl_ids = {doc["id"] for doc in active_docs}

    cursor.execute("SELECT paperless_id, nextcloud_path FROM file_map")
    tracked_rows = cursor.fetchall()

    for paperless_id, stored_path in tracked_rows:
        if paperless_id not in active_pl_ids:
            print(f"[PAPERLESS DELETE] Document #{paperless_id} removed from Paperless. Removing from Nextcloud: '{stored_path}'")

            file_url = f"{nc_docs_dav_url}/{urllib.parse.quote(stored_path)}"
            del_res = requests.delete(file_url, auth=nc_auth, timeout=15)

            if del_res.ok or del_res.status_code == 404:
                cursor.execute("DELETE FROM file_map WHERE paperless_id = ?", (paperless_id,))
                db.commit()
                print(f"[CLEANUP] Removed Nextcloud file '{stored_path}' and cleared record from sync.db")
            else:
                print(f"[ERROR] Failed to delete Nextcloud file at '{stored_path}': {del_res.text}")