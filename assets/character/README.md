# 人物素材来源约束

- `character-box-open-transparent.webm` 是人物外观母版。
- `character-idle.webp` 是该开箱动画的闭箱首帧。
- `character-animation-forward-frame.png` 来自同一开箱动画的 1.20 秒帧，仅用于提取完整虹膜。
- `assets/illustrations/aihuishou-ops-mascot-v4-wanwu.png` 仅作为左右眼白轮廓、眼角与眼皮纹理参考；不会替换人物脸型、身体、外套或纸箱。
- `character-idle-eye-base.webp`、`character-idle-forward.webp`、`left-iris.webp`、`right-iris.webp` 由 `scripts/generate_character_eye_assets.py` 生成。
- 首页显示层仍只引用 `assets/character/` 内的对齐素材，禁止直接显示旧静态人物，也禁止用 CSS 绘制瞳孔。
- `character-columbia-easter-egg.png` 是三连击彩蛋的独立透明人物设计稿；尚未接入首页交互，不替换上述现用素材。

重新生成命令：`PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 scripts/generate_character_eye_assets.py`
