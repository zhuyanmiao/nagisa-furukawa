#!/usr/bin/env python3
"""Clean and repack the generated candidate; never overwrite the source artwork.

Requires Pillow. Bands are manual extraction guides for this candidate, not a
general sprite-sheet detector. Where generated poses overlap, cropping cannot
reconstruct missing anatomy: the report retains those warnings for visual QA.
"""
import json
from collections import deque
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "artwork/spritesheet-v2.png"
OUTPUT = ROOT / "artwork/spritesheet-v2-packed.webp"
CONTACT = ROOT / "artwork/contact-sheet-v2.png"
REPORT = ROOT / "artwork/validation.json"
COUNTS = [6, 8, 8, 4, 5, 8, 6, 6, 6]
NAMES = ["idle", "running-right", "running-left", "waving", "jumping",
         "failed", "waiting", "running", "review"]
# Valleys inspected in the alpha profile, near the original approximate bands.
BANDS = [0, 151, 309, 467, 626, 787, 931, 1082, 1230, 1385]
CELL_W, CELL_H, PADDING = 192, 208, 8


def clean_alpha(image):
    """Remove near-invisible color debris and stabilize opaque painted areas."""
    out = image.copy()
    alpha = out.getchannel("A").point(
        lambda a: 0 if a <= 8 else (255 if a >= 248 else a))
    out.putalpha(alpha)
    # Clear RGB only where fully transparent to avoid hidden matte colors.
    pixels = out.load()
    for y in range(out.height):
        for x in range(out.width):
            if pixels[x, y][3] == 0:
                pixels[x, y] = (0, 0, 0, 0)
    return out


def components(image):
    alpha = image.getchannel("A")
    px = alpha.load()
    seen = set()
    result = []
    for y in range(image.height):
        for x in range(image.width):
            if (x, y) in seen or px[x, y] == 0:
                continue
            todo = deque([(x, y)])
            seen.add((x, y))
            points = []
            while todo:
                xx, yy = todo.popleft()
                points.append((xx, yy))
                for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    nx, ny = xx + dx, yy + dy
                    if (0 <= nx < image.width and 0 <= ny < image.height
                            and (nx, ny) not in seen and px[nx, ny] > 0):
                        seen.add((nx, ny))
                        todo.append((nx, ny))
            result.append(points)
    return sorted(result, key=len, reverse=True)


def bbox(points):
    return (min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points) + 1, max(p[1] for p in points) + 1)


def bbox_distance(a, b):
    return max(max(a[0] - b[2], b[0] - a[2], 0),
               max(a[1] - b[3], b[1] - a[3], 0))


def keep_character(tile):
    groups = components(tile)
    if not groups:
        raise ValueError("An expected animation frame is empty")
    main_bounds = bbox(groups[0])
    kept = [groups[0]]
    # Preserve nearby detached painted props/detail; remove isolated fragments
    # from neighbors and tiny specks. No shape synthesis is performed.
    for group in groups[1:]:
        bounds = bbox(group)
        boundary_fragment = ((bounds[1] == 0 or bounds[3] == tile.height)
                             and bounds[3] - bounds[1] <= 12)
        if (len(group) >= 16 and not boundary_fragment
                and bbox_distance(main_bounds, bounds) <= 14):
            kept.append(group)
    mask = Image.new("L", tile.size)
    mp = mask.load()
    for group in kept:
        for x, y in group:
            mp[x, y] = tile.getpixel((x, y))[3]
    out = tile.copy()
    out.putalpha(mask)
    return out, sum(map(len, groups)) - sum(map(len, kept))


def edge_has_content(image):
    alpha = image.getchannel("A")
    w, h = image.size
    return any(alpha.getpixel((x, y)) for x in range(w) for y in (0, h - 1)) or any(
        alpha.getpixel((x, y)) for y in range(h) for x in (0, w - 1))


def separation_seam(image, col, center):
    """Find a left-to-right gap between neighboring poses, preserving ahoge.

    The path minimizes painted alpha, with a small penalty for leaving the
    manually inspected boundary. It cannot recover genuinely overlapping ink.
    """
    x0, x1 = col * 142, (col + 1) * 142
    lo, hi = center - 12, center + 13
    alpha = image.getchannel("A")
    rows = list(range(lo, hi))
    costs, parents = [], []
    for x in range(x0, x1):
        current, back = [], []
        for index, y in enumerate(rows):
            pixel_cost = alpha.getpixel((x, y)) * 40 + abs(y - center) * 1.5
            if not costs:
                current.append(pixel_cost)
                back.append(index)
            else:
                choices = [(costs[j] + abs(j - index), j)
                           for j in range(max(0, index - 3), min(len(rows), index + 4))]
                best, previous = min(choices)
                current.append(pixel_cost + best)
                back.append(previous)
        costs = current
        parents.append(back)
    index = min(range(len(rows)), key=lambda j: costs[j])
    path = []
    for column in range(x1 - x0 - 1, -1, -1):
        path.append(rows[index])
        index = parents[column][index]
    return list(reversed(path))


def main():
    source = Image.open(SOURCE).convert("RGBA")
    if source.size != (1136, 1385):
        raise ValueError("Extraction bands were reviewed only for the 1136×1385 candidate")
    cleaned = clean_alpha(source)
    # A clean original ahoge from idle frame 1 replaces only clipped upper hair
    # in waiting/review; its four-pixel scalp overlap avoids a hard join.
    donor = cleaned.crop((142 + 49, 4, 142 + 106, 24))
    lower_splits = {5: [928, 927, 927, 928, 928, 930, 931, 931],
                    7: [1229, 1230, 1230, 1228, 1229, 1230]}
    atlas = Image.new("RGBA", (CELL_W * 8, CELL_H * 9))
    report = {"source": str(SOURCE.relative_to(ROOT)), "source_size": list(source.size),
              "output": str(OUTPUT.relative_to(ROOT)), "required_size": [1536, 1872],
              "active_counts": COUNTS, "alpha_cutoff": 8, "opaque_alpha_floor": 248,
              "extraction_bands": BANDS, "minimum_padding": PADDING,
              "boundary_repairs": "Per-column lower splits remove next-row hair. Waiting/review clipped ahoge replaced using original idle frame 1 crop [191,4,248,24], including 4px scalp overlap. No invented anatomy.",
              "status": "packed_candidate_requires_visual_QA", "frames": [],
              "limitations": ["Generated animation poses are preserved; no intermediate motion was invented.",
                               "Touching or overlapping poses in the source cannot be reconstructed by cropping.",
                               "A frame touching an extraction band is flagged; inspect hair and feet manually."]}
    for row, count in enumerate(COUNTS):
        frames = []
        for col in range(count):
            x0, x1 = round(col * source.width / 8), round((col + 1) * source.width / 8)
            top_edge = BANDS[row]
            bottom_edge = lower_splits[row][col] if row in lower_splits else BANDS[row + 1]
            donor_base = None
            donor_center = None
            if row == 6:
                donor_base = [944, 941, 941, 944, 944, 944][col]
                donor_center = [83, 83, 83, 82, 83, 84][col]
                top_edge = donor_base - 18
            elif row == 8:
                donor_base = [1241, 1240, 1240, 1241, 1241, 1241][col]
                donor_center = [75, 73, 73, 73, 73, 72][col]
                top_edge = donor_base - 18
            crop_box = (x0, top_edge, x1, bottom_edge)
            tile = cleaned.crop(crop_box)
            if donor_base is not None:
                tile.paste((0, 0, 0, 0), (0, 0, tile.width, donor_base - 2 - top_edge))
                tile.alpha_composite(donor, (donor_center - donor.width // 2, 0))
            character, removed = keep_character(tile)
            bounds = character.getchannel("A").getbbox()
            warnings = []
            if edge_has_content(character):
                warnings.append("source_content_touches_extraction_boundary_possible_clipping")
            frames.append((character.crop(bounds), crop_box, bounds, removed, warnings))
        # One common scale per row avoids changing a character's size each frame.
        max_w = max(item[0].width for item in frames)
        max_h = max(item[0].height for item in frames)
        scale = min((CELL_W - 2 * PADDING) / max_w,
                    (CELL_H - 2 * PADDING) / max_h)
        for col, (sprite, crop_box, bounds, removed, warnings) in enumerate(frames):
            width, height = round(sprite.width * scale), round(sprite.height * scale)
            resized = sprite.resize((width, height), Image.Resampling.LANCZOS)
            # Resampling can introduce low-alpha halos; apply the same policy.
            resized = clean_alpha(resized)
            left = col * CELL_W + (CELL_W - width) // 2
            top = row * CELL_H + CELL_H - PADDING - height
            atlas.alpha_composite(resized, (left, top))
            report["frames"].append({"row": row, "state": NAMES[row], "column": col,
                                     "source_crop": list(crop_box), "source_bbox_in_crop": list(bounds),
                                     "removed_isolated_pixels": removed, "row_scale": scale,
                                     "destination_bbox": [left, top, left + width, top + height],
                                     "upper_hair_donor_repair": row in (6, 8),
                                     "warnings": warnings})
    OUTPUT.parent.mkdir(exist_ok=True)
    atlas.save(OUTPUT, format="WEBP", lossless=True, exact=True)
    # Validate the encoded file, not just the in-memory image.
    encoded = Image.open(OUTPUT).convert("RGBA")
    blank_cells = []
    padding_failures = []
    for row in range(9):
        for col in range(8):
            tile = encoded.crop((col * CELL_W, row * CELL_H,
                                 (col + 1) * CELL_W, (row + 1) * CELL_H))
            b = tile.getchannel("A").getbbox()
            if col >= COUNTS[row]:
                blank_cells.append({"row": row, "column": col, "completely_transparent": b is None})
            elif b is None or min(b[0], b[1], CELL_W - b[2], CELL_H - b[3]) < PADDING:
                padding_failures.append([row, col])
    report["validation"] = {"encoded_size": list(encoded.size), "encoded_mode": encoded.mode,
                            "size_matches": encoded.size == (1536, 1872),
                            "active_frames": len(report["frames"]), "blank_cells": blank_cells,
                            "all_blank_cells_clear": all(c["completely_transparent"] for c in blank_cells),
                            "padding_failures": padding_failures,
                            "all_active_cells_have_8px_padding": not padding_failures,
                            "frames_with_source_clipping_warning": sum(bool(f["warnings"]) for f in report["frames"])}
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if (not report["validation"]["size_matches"]
            or report["validation"]["active_frames"] != 57
            or not report["validation"]["all_blank_cells_clear"]
            or not report["validation"]["all_active_cells_have_8px_padding"]):
        raise SystemExit("Encoded atlas validation failed; inspect artwork/validation.json")
    label_h = 26
    contact = Image.new("RGB", (1536, (CELL_H + label_h) * 9), "#f4f0ec")
    draw = ImageDraw.Draw(contact)
    font = ImageFont.load_default(size=16)
    for row in range(9):
        y0 = row * (CELL_H + label_h)
        draw.rectangle((0, y0, 1535, y0 + label_h - 1), fill="#352e2a")
        draw.text((8, y0 + 4), f"{row}: {NAMES[row]} / {COUNTS[row]} frames - packed candidate", fill="white", font=font)
        for col in range(8):
            x0 = col * CELL_W
            for yy in range(0, CELL_H, 16):
                for xx in range(0, CELL_W, 16):
                    color = "#e4dfda" if ((xx // 16 + yy // 16) % 2) else "#fffdf9"
                    draw.rectangle((x0 + xx, y0 + label_h + yy,
                                    min(x0 + xx + 15, x0 + CELL_W - 1),
                                    min(y0 + label_h + yy + 15, y0 + label_h + CELL_H - 1)), fill=color)
            tile = encoded.crop((col * CELL_W, row * CELL_H, (col + 1) * CELL_W, (row + 1) * CELL_H))
            contact.paste(tile, (x0, y0 + label_h), tile)
            draw.rectangle((x0, y0 + label_h, x0 + CELL_W - 1, y0 + label_h + CELL_H - 1), outline="#9eaea2")
    contact.save(CONTACT)
    print(json.dumps(report["validation"], indent=2))
    print("WARNING: Source-boundary warnings require visual review; packing cannot restore overlap or missing anatomy.")


if __name__ == "__main__":
    main()
