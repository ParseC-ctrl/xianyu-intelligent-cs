#!/usr/bin/env python3
"""Public source must exclude private business datasets and runtime exports."""
import json
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
files = subprocess.check_output(['git', 'ls-files', '-z'], cwd=root).decode().split('\0')
blocked_suffixes = {'.xlsx', '.xls', '.xlsm', '.csv', '.tsv', '.docx', '.pdf', '.zip', '.db', '.sqlite', '.sqlite3'}
blocked_names = {'customer-faqs.json', 'shop_snapshot.json', 'material_knowledge.json', 'knowledge.json', 'customer.env', 'saas.env', 'windows-admin.json'}
for name in filter(None, files):
    path = Path(name)
    assert path.suffix.lower() not in blocked_suffixes, f'private dataset or export tracked: {name}'
    assert path.name not in blocked_names, f'private runtime file tracked: {name}'
    assert not set(path.parts) & {'deployment-assets', '.local', 'runtime-data', 'node_modules'}, name
assert json.loads((root/'config/customer-faqs.example.json').read_text()) == {'faqs': []}
print('public-data contract: business datasets, runtime exports and real FAQ excluded')
