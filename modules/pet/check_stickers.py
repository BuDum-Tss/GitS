from PIL import Image
import os
import sys

NAMES = ['idle','thinking','angry','question',
         'action','looking','resting',
         'threat','happy','sitting',
         'sad','scared','proud','surprised']

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'sdk'))
from gits_sdk import resources  # noqa: E402

DIR = str(resources.module_dir('pet') / 'stickers')

issues = []
for name in NAMES:
    p = os.path.join(DIR, name + '.png')
    img = Image.open(p).convert('RGBA')
    w, h = img.size
    alpha = img.split()[3]
    bbox = alpha.getbbox()
    # bbox должен быть примерно одинаковый и занимать разумную часть картинки
    if bbox is None:
        issues.append(f"  ПУСТО: {name}")
        continue
    fill_ratio = (bbox[2]-bbox[0]) * (bbox[3]-bbox[1]) / (w * h)
    print(f"  {name:12s}: {w}x{h}  bbox={bbox}  заполнение={fill_ratio:.0%}")
    if fill_ratio < 0.05:
        issues.append(f"  СЛИШКОМ МАЛО пикселей: {name} ({fill_ratio:.0%})")

# HTML превью
html_lines = [
    '<html><body style="background:#222;font-family:monospace;padding:12px">',
    '<h2 style="color:#0f0">Sticker Check</h2>',
    '<div style="display:flex;flex-wrap:wrap;gap:12px">',
]
for name in NAMES:
    html_lines.append(
        f'<div style="text-align:center">'
        f'<div style="background:repeating-conic-gradient(#333 0% 25%,#444 0% 50%) 0 0/16px 16px">'
        f'<img src="{name}.png"></div>'
        f'<div style="color:#aaa;font-size:11px;margin-top:4px">{name}</div></div>'
    )
html_lines += ['</div></body></html>']

out = os.path.join(DIR, 'preview.html')
with open(out, 'w', encoding='utf-8') as f:
    f.write('\n'.join(html_lines))
print(f'\nПревью: {out}')

if issues:
    print('\nПРОБЛЕМЫ:')
    for i in issues:
        print(i)
else:
    print('\nВсе стикеры OK.')
