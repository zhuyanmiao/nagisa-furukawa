#!/usr/bin/env python3
"""Export a static and animated 3x3 overview from the installed-format V4 atlas."""
import argparse
import json
import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
COUNTS = [6, 8, 8, 4, 5, 8, 6, 6, 6]
LABELS_ZH = ['待机', '向右小跑', '向左小跑', '挥手', '大团子托起', '失落', '等待', '跑动', '看电脑']
LABELS_EN = ['Idle', 'Run right', 'Run left', 'Wave', 'Dango float', 'Sad', 'Waiting', 'Running', 'Review']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font', type=Path, help='Optional Chinese TrueType/OpenType font')
    args = parser.parse_args()
    candidates = ([args.font] if args.font else []) + [
        Path('/System/Library/Fonts/STHeiti Medium.ttc'),
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
        Path('C:/Windows/Fonts/msyh.ttc'),
    ]
    available = next((path for path in candidates if path.is_file()), None)
    font = ImageFont.truetype(str(available), 22) if available else ImageFont.load_default(size=20)
    labels = LABELS_ZH if available else LABELS_EN
    with Image.open(ROOT / 'nagisa-furukawa-v4/spritesheet.webp') as opened:
        atlas = opened.convert('RGBA')
    if atlas.size != (1536, 1872):
        raise ValueError('Expected a 1536x1872 atlas')
    base = Image.new('RGB', (768, 924), '#f5eee7')
    draw = ImageDraw.Draw(base)
    tiles = {}
    for row, label in enumerate(labels):
        x, y = (row % 3) * 256, (row // 3) * 308
        draw.rounded_rectangle((x + 5, y + 5, x + 250, y + 301), radius=16, fill='#fffaf5')
        box = draw.textbbox((0, 0), label, font=font)
        draw.text((x + (256 - (box[2] - box[0])) // 2, y + 18), label, font=font, fill='#634b3c')
        tiles[row] = [atlas.crop((i * 192, row * 208, (i + 1) * 192, (row + 1) * 208))
                      .resize((240, 260), Image.Resampling.LANCZOS) for i in range(COUNTS[row])]
    # 120 frames return all nine independent loops to their starting phases.
    cycle = 1
    for count in COUNTS:
        cycle = cycle * count // math.gcd(cycle, count)
    frames = []
    for index in range(cycle):
        frame = base.copy()
        for row, count in enumerate(COUNTS):
            tile = tiles[row][index % count]
            frame.paste(tile, ((row % 3) * 256 + 8, (row // 3) * 308 + 45), tile)
        frames.append(frame)
    directory = ROOT / 'previews'
    directory.mkdir(exist_ok=True)
    frames[0].save(directory / 'all-actions-v4.png')
    destination = directory / 'all-actions-v4.webp'
    # Only this overview is compressed; the actual pet atlas remains lossless.
    frames[0].save(destination, save_all=True, append_images=frames[1:],
                   duration=167, loop=0, quality=92, method=4)
    with Image.open(destination) as check:
        if check.n_frames != cycle or check.size != (768, 924):
            raise ValueError('Animated overview dimensions or frame count are incorrect')
    print(json.dumps({'path': str(destination), 'frames': cycle,
                      'bytes': destination.stat().st_size}, ensure_ascii=False))


if __name__ == '__main__':
    main()
