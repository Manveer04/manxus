import json, pathlib
src = pathlib.Path('/mnt/YourPool/manxus/sessions/tiktok-cookies.json')
raw = json.loads(src.read_text())
storage_state = {
    'cookies': [
        {
            'name':     c['name'],
            'value':    c['value'],
            'domain':   c['domain'],
            'path':     c.get('path', '/'),
            'expires':  c.get('expirationDate', -1),
            'httpOnly': c.get('httpOnly', False),
            'secure':   c.get('secure', False),
            'sameSite': 'Lax',
        }
        for c in raw
    ],
    'origins': []
}
out = pathlib.Path('/mnt/YourPool/manxus/sessions/tiktok.json')
out.write_text(json.dumps(storage_state, indent=2))
print('Done - ' + str(len(storage_state['cookies'])) + ' cookies saved')