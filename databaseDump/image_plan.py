"""上传前按注册物品分组去重，中文别名与变体图片直接指向实体图片。"""

import hashlib
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image


def pixel_hash(path):
    """比较完整 RGBA 像素和尺寸；APNG 同时比较完整帧及播放时序。"""
    with Image.open(path) as image:
        if getattr(image, 'is_animated', False):
            digest = hashlib.sha256(b'animated\0')
            default_image = bool(image.info.get('default_image', False))
            playback = (image.width, image.height, image.n_frames,
                        image.info.get('loop', 0), default_image)
            digest.update(json.dumps(playback).encode())
            for index in range(image.n_frames):
                image.seek(index)
                # Pillow 合成 disposal/blend 后的完整画布；编码方式不同但播放像素相同可合并。
                duration = None if default_image and index == 0 else image.info.get('duration', 0)
                digest.update(json.dumps(duration).encode() + b'\0')
                digest.update(image.convert('RGBA').tobytes())
            return digest.hexdigest()
        digest = hashlib.sha256(f'rgba:{image.width}:{image.height}:'.encode())
        digest.update(image.convert('RGBA').tobytes())
        return digest.hexdigest()


def registry_and_damage(raw):
    """只移除末尾数值 metadata；不按下划线猜测 NBT 后缀。"""
    registry, separator, damage = raw.rpartition(':')
    if separator and ':' in registry and damage.lstrip('-').isdigit():
        return registry, int(damage)
    return raw, 0


def build_plan(items, icon_records, icons_dir, name_redirects):
    """items 使用原始 advitempanel 记录，key 为无 NBT 哈希的真实身份。"""
    from item_names import canonical_file

    icons_dir = Path(icons_dir)
    physical = {path.name for path in icons_dir.glob('*.png')}
    records = {row['shortKey']: row for row in icon_records}
    owners = defaultdict(set)
    candidates = defaultdict(dict)
    for item in items:
        raw = item.get('key', '')
        registry, damage = registry_and_damage(raw)
        if ':' not in registry:
            continue
        record = records.get(item.get('shortKey'), {})
        icon = record.get('iconName') or item.get('icon')
        if icon not in physical:
            continue
        nbt = item.get('nbt')
        has_nbt = bool(nbt and str(nbt).strip() != '{}')
        images = [('static', icon)]
        dynamic = icon.removesuffix('.png') + '_dynamic.png'
        if record.get('frameCount', 0) > 1 and dynamic in physical:
            images.append(('animation', dynamic))
        for role, filename in images:
            owners[filename].add(registry)
            rank = (has_nbt, damage != 0, damage, item.get('shortKey', raw), filename)
            previous = candidates[(registry, role)].get(filename)
            if previous is None or rank < previous:
                candidates[(registry, role)][filename] = rank

    aliases = {}
    groups = []
    hashed = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        for (registry, role), variants in sorted(candidates.items()):
            # 被不同注册物品共同使用的物理文件保持原状，避免跨物品合并。
            variants = {icon: rank for icon, rank in variants.items() if len(owners[icon]) == 1}
            if len(variants) < 2:
                continue
            by_hash = defaultdict(list)
            names = sorted(variants, key=variants.get)
            hashes = pool.map(pixel_hash, (icons_dir / icon for icon in names))
            for icon, digest in zip(names, hashes):
                by_hash[digest].append(icon)
                hashed += 1
                if hashed % 10000 == 0:
                    print(f'Hashing item images: {hashed:,}', flush=True)
            for digest, names in by_hash.items():
                if len(names) < 2:
                    continue
                target, *duplicates = names
                aliases.update((name, target) for name in duplicates)
                groups.append({'registry': registry, 'role': role, 'sha256': digest,
                               'target': target, 'duplicates': duplicates})

    uploads = physical - aliases.keys()
    redirects = {'File:' + canonical_file(name): target for name, target in aliases.items()}
    for title, icon in name_redirects.items():
        title = 'File:' + canonical_file(title.removeprefix('File:'))
        target = aliases.get(icon, icon)
        if title == 'File:' + canonical_file(target):
            continue
        if title in redirects and redirects[title] != target:
            raise ValueError(f'中文名与物品图片重定向冲突: {title}')
        redirects[title] = target
    if set(redirects.values()) - uploads:
        raise ValueError('重定向目标必须为当前实体图片')
    target_titles = {'File:' + canonical_file(icon) for icon in redirects.values()}
    if target_titles & redirects.keys():
        raise ValueError('图片计划包含二级重定向或标题冲突')
    return {'schemaVersion': 1, 'physicalCount': len(physical), 'hashedCount': hashed,
            'uploadCount': len(uploads), 'deduplicatedCount': len(aliases),
            'uploadIcons': sorted(uploads), 'redirects': dict(sorted(redirects.items())),
            'groups': groups}


def prepare_plan(source, resolved_csv):
    """每次上传/创建重定向前重新计算当前图片，计划仅写入本地状态目录。"""
    from image_version import redirect_map

    source = Path(source)
    redirects = redirect_map(resolved_csv)
    # 名称映射已校验该文件的哈希；独立模组可能使用完整 dumps 的关联图标数据。
    report = json.loads(Path(resolved_csv).with_name('image-name-resolution.json').read_text('utf-8'))
    plan = build_plan(
        json.loads((source / 'advitempanel.json').read_text('utf-8'))['shortKey'],
        json.loads(Path(report['sourceIconsPath']).read_text('utf-8'))['shortKey'],
        source / 'icons', redirects)
    Path(resolved_csv).with_name('image-upload-plan.json').write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), 'utf-8')
    print(f"Image plan: {plan['physicalCount']:,} files; "
          f"{plan['deduplicatedCount']:,} duplicates; {plan['uploadCount']:,} physical uploads")
    return plan
