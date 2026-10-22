# Nextcloud to Paperless-ngx Bidirectional Sync Bridge
A Python-based daemon that orchestrates a bidirectional synchronization between Nextcloud (via WebDAV) and Paperless-ngx (via REST API). The bridge tracks file states using a local SQLite database to prevent infinite sync loops, handles document renames/moves, creates deep nested directory structures automatically, and filters for PDF files.

# Features
- Bidirectional Syncing: Ingests new Nextcloud PDFs into Paperless, and maps new Paperless documents back to Nextcloud.
- Soft-Linking: Handles existing populated libraries by mapping matching files (by name and path) into the database without creating duplicates.
- Deep Folder Recursion: Automatically translates Paperless tags into nested Nextcloud folders (e.g., Financial/2026/Q1), creating the WebDAV directories if they don't exist.
- PDF-Only Filtering: Safely ignores non-PDF files (Word, Excel, Images) to prevent corrupting Nextcloud with incorrectly extension-mapped files.
- Stateful Tracking: Uses an SQLite database (sync.db) to track immutable oc:fileids from Nextcloud and document IDs from Paperless to manage bidirectional renames and deletions.

# Docker Compose Configuration
The sync bridge is deployed alongside your Nextcloud and Paperless instances using Docker Compose. The configuration maps the required environment variables to authorize both APIs.

Example:
```yaml
sync_bridge:
    container_name: lab_sync_bridge
    image: ghcr.io/rangodemango/nextcloud-paperless-sync:latest
    restart: unless-stopped
    depends_on:
      - paperless_webserver
      - nextcloud_app
    environment:
      PAPERLESS_API_URL: http://paperless:8000/api
      PAPERLESS_API_TOKEN: paperless api token
      NEXTCLOUD_BASE_URL: http://nextcloud_app
      NEXTCLOUD_USER: "nextcloud username"
      NEXTCLOUD_PASS: "nextcloud token"
      NEXTCLOUD_FOLDER: "Documents"
      POLL_INTERVAL : Poll interval in seconds
    volumes:
      - ./sync_bridge/state:/app/state
      - ./sync_bridge:/app
```

## Environment Variable Breakdown
- PAPERLESS_API_URL: The internal Docker network routing to the Paperless API. Because the Paperless container uses the paperless network alias, http://paperless:8000/api resolves directly without leaving the internal Docker bridge.
- PAPERLESS_API_TOKEN: Your static authentication token generated in the Paperless Django admin panel.
- NEXTCLOUD_BASE_URL: The internal hostname for your Nextcloud app container (http://nextcloud_app).
- NEXTCLOUD_USER / NEXTCLOUD_PASS: Credentials for the Nextcloud account holding the documents. For production, these should ideally reference Docker secrets instead of plain text.
- NEXTCLOUD_FOLDER: The base WebDAV directory inside Nextcloud where the sync operations will occur. In this case, it is locked to the Documents folder.
- POLL_INTERVAL: How often the daemon queries both APIs for changes, set to 10 seconds.

## Volume Mapping
`yaml
./sync_bridge/state:/app/state:
`
Binds the local state folder to the container. This ensures the sync.db SQLite database survives container rebuilds and restarts. Without this, the daemon would lose its tracking memory every time the container restarted, resulting in duplicated files.

`yaml
./sync_bridge:/app:
`
Binds the source code directory directly into the container, allowing you to edit the Python files locally and apply changes instantly by restarting the container.