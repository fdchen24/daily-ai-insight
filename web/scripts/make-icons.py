"""生成 PWA 图标（192/512 PNG）。

为什么不直接用 favicon.svg：Chrome 认 SVG 图标，但 iOS 把站点添加到主屏时要
PNG，而"添加到主屏"正是 DESIGN 7.3 对抗 Safari 7 天清除存储的唯一手段——图标
缺失会让安装提示变得不可信。

图形是三根递增的柱子（每日递进的意思），刻意留出 20% 边距：maskable 图标会被
系统裁成圆形或圆角方形，内容压边就会被切掉。

    python3 web/scripts/make-icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "public" / "icons"
BG = (11, 15, 23)
FG = (56, 189, 248)


def render(size: int) -> Image.Image:
    # 4 倍超采样再缩回去：PIL 没有抗锯齿绘制，直接画圆角会有锯齿
    scale = 4
    canvas = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    edge = size * scale
    draw.rounded_rectangle((0, 0, edge - 1, edge - 1), radius=edge * 0.22, fill=BG)

    pad = edge * 0.26           # 安全区：maskable 裁切不会碰到柱子
    width = edge * 0.12
    gap = (edge - 2 * pad - 3 * width) / 2
    for index, ratio in enumerate((0.38, 0.66, 1.0)):
        left = pad + index * (width + gap)
        top = edge - pad - (edge - 2 * pad) * ratio
        draw.rounded_rectangle((left, top, left + width, edge - pad),
                               radius=width / 2, fill=FG)
    return canvas.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        path = OUT / f"icon-{size}.png"
        render(size).save(path)
        print(f"写出 {path.relative_to(OUT.parent.parent)}（{path.stat().st_size} 字节）")


if __name__ == "__main__":
    main()
