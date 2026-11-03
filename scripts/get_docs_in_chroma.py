"""Diagnostic script: display all documents indexed in the active Chroma collection.

Connects directly to the ChromaDB persistent store and, if available, enriches
missing titles by querying the Zotero SQLite database. Prints a tabular summary
of every ingested document showing item key, item ID, content type, chunk count,
and title.

Usage:
    Edit the ``chroma_path``, ``fts_path``, and ``zotero_sqlite`` constants
    at the top of this file to match your local paths, then run::

        uv run python scripts/get_docs_in_chroma.py
"""

import chromadb, sqlite3

chroma_path = '/zocrates-data/chroma'
fts_path = '/zocrates-data/fts_index.db'
zotero_sqlite = '/home/alex/Zotero/Zotero test-user/zotero.sqlite'

client = chromadb.PersistentClient(path=chroma_path)
col = client.get_collection('zotero_library_jinaai_jina_embeddings_v5_text_nano_768d')

all_data = col.get(include=['metadatas'])
metas = all_data['metadatas']

# Group by item_key
items_map = {}
for m in metas:
    key = m.get('item_key', 'Unknown')
    item_id = m.get('item_id', 0)
    title = m.get('document_title', '')
    is_note = m.get('is_note', False)
    content_type = m.get('content_type', 'note' if is_note else 'pdf/html')

    if key not in items_map:
        items_map[key] = {
            'item_id': item_id,
            'title': title,
            'content_type': content_type,
            'chunk_count': 0
        }
    items_map[key]['chunk_count'] += 1
    if not items_map[key]['title'] and title:
        items_map[key]['title'] = title

# For any missing titles or item_ids, query zotero.sqlite if available
if zotero_sqlite:
    try:
        conn = sqlite3.connect(f'file:{zotero_sqlite}?mode=ro', uri=True)
        cursor = conn.cursor()
        for key, info in items_map.items():
            if not info['title'] or info['title'] == 'No Title':
                # Query title from zotero.sqlite
                cursor.execute('''
                    SELECT v.value 
                    FROM items i 
                    JOIN itemData id ON i.itemID = id.itemID 
                    JOIN itemDataValues v ON id.valueID = v.valueID 
                    JOIN fields f ON id.fieldID = f.fieldID 
                    WHERE i.key = ? AND f.fieldName = 'title'
                ''', (key,))
                row = cursor.fetchone()
                if row and row[0]:
                    info['title'] = row[0]
                else:
                    # Try note title or parent title
                    cursor.execute('SELECT itemID FROM items WHERE key = ?', (key,))
                    i_row = cursor.fetchone()
                    if i_row:
                        info['item_id'] = i_row[0]
        conn.close()
    except Exception as e:
        print('SQLite title lookup warning:', e)

print('=== PROCESSED DOCUMENTS IN CHROMA DB ===')
print(f"{"#":<3} | {"Item Key":<10} | {"Item ID":<8} | {"Type":<10} | {"Chunks":<6} | {"Title"}")
print('-' * 95)
for idx, (key, info) in enumerate(sorted(items_map.items(), key=lambda x: x[1]['item_id']), 1):
    title = info['title'] if info['title'] else '(No Title / Note)'
    print(f"{idx:<3} | {key:<10} | {info["item_id"]:<8} | {info["content_type"]:<10} | {info["chunk_count"]:<6} | {title}")