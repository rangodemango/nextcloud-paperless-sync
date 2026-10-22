import os
import urllib.parse
import xml.etree.ElementTree as ET
import requests

def get_nextcloud_file_id(nc_docs_dav_url: str, nc_auth: tuple, rel_path: str) -> str | None:
    """Helper to query a specific file's oc:fileid from Nextcloud via PROPFIND."""
    propfind_body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
        '<d:prop><oc:fileid/></d:prop>'
        '</d:propfind>'
    )
    file_url = f"{nc_docs_dav_url}/{urllib.parse.quote(rel_path)}"
    res = requests.request(
        "PROPFIND",
        file_url,
        auth=nc_auth,
        headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"},
        data=propfind_body.encode("utf-8"),
        timeout=10
    )
    if res.ok:
        ns = {"d": "DAV:", "oc": "http://owncloud.org/ns"}
        root = ET.fromstring(res.content)
        fileid_elem = root.find(".//oc:fileid", ns)
        if fileid_elem is not None and fileid_elem.text:
            return fileid_elem.text
    return None

def scan_nextcloud_docs(nc_docs_dav_url: str, nc_auth: tuple, nc_user: str, nc_folder: str) -> list:
    """Uses WebDAV PROPFIND to list all files with infinite depth along with their immutable oc:fileid."""
    propfind_body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
        '<d:prop><oc:fileid/><d:resourcetype/></d:prop>'
        '</d:propfind>'
    )

    res = requests.request(
        "PROPFIND",
        nc_docs_dav_url,
        auth=nc_auth,
        headers={"Depth": "infinity", "Content-Type": "application/xml; charset=utf-8"},
        data=propfind_body.encode("utf-8"),
        timeout=60
    )
    if res.status_code == 404:
        print(f"[WARN] Nextcloud /{nc_folder} folder not found at {nc_docs_dav_url}. Create it in the Nextcloud UI.")
        return []
    if not res.ok:
        print(f"[ERROR] WebDAV returned {res.status_code}: {res.text[:300]}")
        res.raise_for_status()

    ns = {"d": "DAV:", "oc": "http://owncloud.org/ns"}
    root = ET.fromstring(res.content)
    prefix = f"/remote.php/dav/files/{urllib.parse.quote(nc_user)}/{urllib.parse.quote(nc_folder)}/"

    discovered_files = []
    for response in root.findall("d:response", ns):
        href = urllib.parse.unquote(response.find("d:href", ns).text)
        prop = response.find("d:propstat/d:prop", ns)
        if prop is None:
            continue

        resourcetype = prop.find("d:resourcetype", ns)
        if resourcetype is not None and resourcetype.find("d:collection", ns) is not None:
            continue

        fileid_elem = prop.find("oc:fileid", ns)
        if fileid_elem is None or not fileid_elem.text:
            continue

        if prefix in href:
            rel_path = href.split(prefix, 1)[1].strip("/")
            if "/" in rel_path and rel_path.lower().endswith(".pdf"):
                folder_tag, filename = rel_path.rsplit("/", 1)
                discovered_files.append({
                    "file_id": fileid_elem.text,
                    "rel_path": rel_path,
                    "folder": folder_tag,
                    "filename": filename
                })
    return discovered_files

def process_new_nextcloud_file(nc_docs_dav_url: str, nc_auth: tuple, paperless_url: str, pl_headers: dict, cursor, db, nc_id: str, rel_path: str, folder_tag: str, filename: str, wait_for_task_func):
    """Downloads a new file from Nextcloud, ingests it into Paperless, and maps it in SQLite."""
    print(f"[NEW] Found unregistered file in Nextcloud: {rel_path} (Nextcloud ID: {nc_id})")

    file_url = f"{nc_docs_dav_url}/{urllib.parse.quote(rel_path)}"
    dl_res = requests.get(file_url, auth=nc_auth, timeout=30)
    dl_res.raise_for_status()

    from sync_paperless import get_or_create_tag
    tag_id = get_or_create_tag(paperless_url, pl_headers, folder_tag)
    title = os.path.splitext(filename)[0]
    
    post_res = requests.post(
        f"{paperless_url}/documents/post_document/",
        headers=pl_headers,
        files={"document": (filename, dl_res.content, "application/pdf")},
        data={"title": title, "tags": [tag_id]},
        timeout=30
    )
    post_res.raise_for_status()
    task_uuid = post_res.text.strip().strip('"')
    print(f"[INGEST] Sent '{filename}' to Paperless with tag '{folder_tag}'. Task UUID: {task_uuid}")

    paperless_doc_id = wait_for_task_func(paperless_url, pl_headers, task_uuid)

    if paperless_doc_id:
        cursor.execute(
            "INSERT INTO file_map (paperless_id, nextcloud_file_id, nextcloud_path) VALUES (?, ?, ?)",
            (paperless_doc_id, nc_id, rel_path)
        )
        db.commit()
        print(f"[MAPPED] Registered in sync.db: Paperless ID #{paperless_doc_id} <-> Nextcloud ID {nc_id}")

def process_existing_nextcloud_file(paperless_id: int, nc_id: str, stored_path: str, rel_path: str, folder_tag: str, filename: str, paperless_url: str, pl_headers: dict, cursor, db):
    """Checks if a mapped Nextcloud file was renamed or moved, and updates Paperless accordingly."""
    if stored_path != rel_path:
        print(f"[UPDATE] Detected change for Nextcloud ID {nc_id}: '{stored_path}' -> '{rel_path}'")

        new_title = os.path.splitext(filename)[0]
        from sync_paperless import get_or_create_tag
        tag_id = get_or_create_tag(paperless_url, pl_headers, folder_tag)

        patch_res = requests.patch(
            f"{paperless_url}/documents/{paperless_id}/",
            headers=pl_headers,
            json={"title": new_title, "tags": [tag_id]},
            timeout=15
        )

        if patch_res.ok:
            cursor.execute(
                "UPDATE file_map SET nextcloud_path = ? WHERE nextcloud_file_id = ?",
                (rel_path, nc_id)
            )
            db.commit()
            print(f"[SYNCED] Updated Paperless ID #{paperless_id} title to '{new_title}' and tag to '{folder_tag}'")
        else:
            print(f"[ERROR] Failed to update Paperless ID #{paperless_id}: {patch_res.text}")

def process_deleted_nextcloud_files(cursor, db, scanned_files: list, paperless_url: str, pl_headers: dict, nc_docs_dav_url: str, nc_auth: tuple):
    """Checks SQLite for tracked files that no longer exist in Nextcloud and deletes them from Paperless."""
    active_nc_ids = {item["file_id"] for item in scanned_files}

    cursor.execute("SELECT paperless_id, nextcloud_file_id, nextcloud_path FROM file_map")
    tracked_rows = cursor.fetchall()

    for paperless_id, nc_id, stored_path in tracked_rows:
        if nc_id not in active_nc_ids:
            print(f"[DELETE] File removed from Nextcloud: '{stored_path}' (Paperless ID #{paperless_id})")

            del_res = requests.delete(
                f"{paperless_url}/documents/{paperless_id}/",
                headers=pl_headers,
                timeout=15
            )

            if del_res.ok or del_res.status_code == 404:
                cursor.execute("DELETE FROM file_map WHERE nextcloud_file_id = ?", (nc_id,))
                db.commit()
                print(f"[CLEANUP] Deleted Paperless ID #{paperless_id} and removed from sync.db")
            else:
                print(f"[ERROR] Failed to delete Paperless document #{paperless_id}: {del_res.text}")