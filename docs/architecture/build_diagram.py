#!/usr/bin/env python3
"""Build docs/architecture/architecture.svg.

The diagram is composed from the official Azure Architecture Icons kept, unmodified, in ./icons.
Each icon is embedded as a data URI so the SVG renders everywhere, including GitHub, which does not
load external files referenced from an SVG that is shown through <img>.

    python3 docs/architecture/build_diagram.py
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

HERE = Path(__file__).resolve().parent
ICON_DIR = HERE / "icons"
OUT = HERE / "architecture.svg"

FONT = "'Segoe UI', 'Helvetica Neue', Arial, sans-serif"
ICON = 48  # rendered icon edge, px
INK = "#1f2937"
MUTED = "#5b6573"
LINE = "#4b5563"
ACCENT = "#0078d4"


@dataclass
class Node:
    icon: str
    label: str
    x: float  # icon centre
    y: float
    sub: str = ""

    @property
    def label_lines(self) -> list[str]:
        return self.label.split("\n")

    @property
    def sub_lines(self) -> list[str]:
        return self.sub.split("\n") if self.sub else []

    @property
    def bottom(self) -> float:
        """y of the lowest text line, i.e. where a downward connector may start."""
        return self.y + ICON / 2 + 18 + 15 * (len(self.label_lines) - 1) + 14 * len(self.sub_lines) + 6


@dataclass
class Zone:
    title: str
    x: float
    y: float
    w: float
    h: float
    fill: str = "#f5f9fe"
    stroke: str = "#b7cde8"
    title_color: str = "#0b5cab"
    icon: str | None = None
    dashed: bool = False


@dataclass
class Edge:
    src: str
    dst: str
    src_side: str = "r"
    dst_side: str = "l"
    via: list[tuple[float, float]] = field(default_factory=list)
    step: int | None = None
    label: str = ""
    dashed: bool = False
    label_at: tuple[float, float] | None = None
    label_anchor: str = "middle"
    both: bool = False  # arrowheads at both ends


def anchor(n: Node, side: str) -> tuple[float, float]:
    """Connection point on a node. `side` is r/l/t/b, optionally followed by an offset along that side
    (for example "r-16" is 16 px above the middle of the right edge), so parallel links can be kept apart."""
    offset = float(side[1:] or 0)
    half = ICON / 2 + 5
    return {
        "r": (n.x + half, n.y + offset),
        "l": (n.x - half, n.y + offset),
        "t": (n.x + offset, n.y - half),
        "b": (n.x + offset, n.bottom),
    }[side[0]]


def data_uri(name: str) -> str:
    raw = (ICON_DIR / name).read_bytes()
    return "data:image/svg+xml;base64," + base64.b64encode(raw).decode("ascii")


def text_width(s: str, size: float, bold: bool = False) -> float:
    return len(s) * size * (0.58 if bold else 0.54)


def render(nodes: dict[str, Node], zones: list[Zone], edges: list[Edge], size: tuple[int, int],
           title: str, subtitle: str, desc: str) -> str:
    w, h = size
    out: list[str] = []
    add = out.append
    add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'role="img" aria-labelledby="t d" font-family="{FONT}">')
    add(f'<title id="t">{escape(title)}</title><desc id="d">{escape(desc)}</desc>')
    add('<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" '
        f'orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="{LINE}"/></marker>'
        '<marker id="arrow-start" viewBox="0 0 10 10" refX="1" refY="5" markerWidth="8" markerHeight="8" '
        f'orient="auto"><path d="M10,0 L0,5 L10,10 z" fill="{LINE}"/></marker></defs>')
    add(f'<rect width="{w}" height="{h}" fill="#ffffff"/>')
    add(f'<text x="28" y="40" font-size="22" font-weight="700" fill="{INK}">{escape(title)}</text>')
    add(f'<text x="28" y="62" font-size="13" fill="{MUTED}">{escape(subtitle)}</text>')

    for z in zones:
        dash = ' stroke-dasharray="6 4"' if z.dashed else ""
        add(f'<rect x="{z.x}" y="{z.y}" width="{z.w}" height="{z.h}" rx="10" fill="{z.fill}" '
            f'stroke="{z.stroke}" stroke-width="1.5"{dash}/>')
        tx = z.x + 14
        if z.icon:
            add(f'<image href="{data_uri(z.icon)}" x="{z.x + 10}" y="{z.y + 8}" width="20" height="20"/>')
            tx = z.x + 38
        add(f'<text x="{tx}" y="{z.y + 23}" font-size="13" font-weight="600" fill="{z.title_color}">'
            f'{escape(z.title)}</text>')

    badges: list[str] = []
    for e in edges:
        s, d = nodes[e.src], nodes[e.dst]
        pts = [anchor(s, e.src_side), *e.via, anchor(d, e.dst_side)]
        dash = ' stroke-dasharray="5 4"' if e.dashed else ""
        marks = ' marker-end="url(#arrow)"' + (' marker-start="url(#arrow-start)"' if e.both else "")
        path = "M" + " L".join(f"{px:.1f},{py:.1f}" for px, py in pts)
        add(f'<path d="{path}" fill="none" stroke="{LINE}" stroke-width="1.6" '
            f'stroke-linejoin="round"{dash}{marks}/>')
        if e.step or e.label:
            # default label position: middle of the longest segment
            seg = max(zip(pts, pts[1:]), key=lambda p: abs(p[0][0] - p[1][0]) + abs(p[0][1] - p[1][1]))
            lx, ly = e.label_at or ((seg[0][0] + seg[1][0]) / 2, (seg[0][1] + seg[1][1]) / 2)
            text = e.label
            tw = text_width(text, 12) if text else 0
            bw = (24 if e.step else 0) + tw + (6 if text else 0)
            x0 = lx - bw / 2 if e.label_anchor == "middle" else lx
            parts = [f'<rect x="{x0 - 3:.1f}" y="{ly - 11:.1f}" width="{bw + 6:.1f}" height="22" rx="11" '
                     'fill="#ffffff" fill-opacity="0.92"/>']
            cx = x0 + 10
            if e.step:
                parts.append(f'<circle cx="{cx:.1f}" cy="{ly:.1f}" r="10" fill="{ACCENT}"/>'
                             f'<text x="{cx:.1f}" y="{ly + 4:.1f}" font-size="12" font-weight="700" '
                             f'fill="#ffffff" text-anchor="middle">{e.step}</text>')
            if text:
                tx = x0 + (24 if e.step else 0)
                parts.append(f'<text x="{tx:.1f}" y="{ly + 4:.1f}" font-size="12" fill="{INK}">'
                             f'{escape(text)}</text>')
            badges.append("".join(parts))

    for n in nodes.values():
        add(f'<image href="{data_uri(n.icon)}" x="{n.x - ICON / 2}" y="{n.y - ICON / 2}" '
            f'width="{ICON}" height="{ICON}"/>')
        ty = n.y + ICON / 2 + 17
        for i, line in enumerate(n.label_lines):
            add(f'<text x="{n.x}" y="{ty + 15 * i}" font-size="13" font-weight="600" fill="{INK}" '
                f'text-anchor="middle">{escape(line)}</text>')
        ty += 15 * (len(n.label_lines) - 1)
        for i, line in enumerate(n.sub_lines):
            add(f'<text x="{n.x}" y="{ty + 14 * (i + 1)}" font-size="11" fill="{MUTED}" '
                f'text-anchor="middle">{escape(line)}</text>')

    out.extend(badges)
    add("</svg>")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------------------
# Diagram definition
# --------------------------------------------------------------------------------------
NODES = {
    "users": Node("10230-icon-service-Users.svg", "Users", 90, 330, "any browser"),
    "web": Node("10035-icon-service-App-Services.svg", "Azure App Service", 380, 330, "MomentLens web app\nFastAPI API + React UI"),
    "vi": Node("01800-icon-service-Azure-Video-Indexer.svg", "Azure AI Video Indexer", 740, 170, "shots, transcript, OCR,\nlabels, keyframes"),
    "job": Node("02989-icon-service-Container-Apps-Environments.svg", "Azure Container Apps job", 740, 400, "vi-poller: polls Video\nIndexer until it is done"),
    "pe": Node("02579-icon-service-Private-Endpoints.svg", "Private endpoints", 540, 560, "blob + table"),
    "blob": Node("10086-icon-service-Storage-Accounts.svg", "Azure Storage account", 740, 560, "Blob: videos, insights, thumbnails\nTable: job state"),
    "aoai": Node("03438-icon-service-Azure-OpenAI.svg", "Azure OpenAI", 1050, 170, "chat, embeddings,\nvision judge"),
    "search": Node("10044-icon-service-AI-Search.svg", "Azure AI Search", 1050, 400, "hybrid + semantic +\nvector moments index"),
    "mi": Node("10227-icon-service-Managed-Identities.svg", "Managed identity", 320, 745, "Microsoft Entra ID access,\nno keys or passwords"),
    "vnet": Node("10061-icon-service-Virtual-Networks.svg", "Virtual network", 520, 745, "VNet integration,\nprivate DNS"),
    "acr": Node("10105-icon-service-Container-Registries.svg", "Container Registry", 720, 745, "api + job images,\npulled by identity"),
    "appi": Node("00012-icon-service-Application-Insights.svg", "Application Insights", 930, 745, "telemetry and the cost &\nlatency workbook"),
    "log": Node("00009-icon-service-Log-Analytics-Workspaces.svg", "Log Analytics", 1140, 745, "container and job logs"),
}

ZONES = [
    Zone("Azure resource group", 200, 90, 1090, 560, fill="#ffffff", stroke="#9db8d6",
         icon="10007-icon-service-Resource-Groups.svg", dashed=True),
    Zone("Platform services used by every component", 200, 672, 1090, 150, fill="#f7f7f9", stroke="#d3d6dc",
         title_color="#4b5563"),
]

EDGES = [
    Edge("users", "web", step=1, label="HTTPS", both=True),
    Edge("web", "pe", src_side="b", dst_side="l", via=[(380, 560)], step=2, label="upload \u00b7 stream", both=True),
    Edge("pe", "blob", both=True),
    Edge("web", "vi", src_side="r-16", via=[(560, 314), (560, 170)], step=3, label="submit video",
         label_at=(560, 246)),
    Edge("web", "job", src_side="r+16", via=[(640, 346), (640, 400)], label="start job", label_at=(640, 374)),
    Edge("job", "vi", src_side="t", dst_side="b", step=4, label="poll", label_at=(740, 290), both=True),
    Edge("job", "blob", src_side="b", dst_side="t", label="insights JSON", label_at=(740, 505)),
    Edge("web", "aoai", src_side="t", dst_side="t", via=[(380, 110), (1050, 110)], step=5,
         label="enrich \u00b7 embed \u00b7 judge"),
    Edge("web", "search", via=[(980, 330), (980, 400)], step=6, label="index \u00b7 hybrid query",
         label_at=(860, 330), both=True),
]


def main() -> None:
    svg = render(NODES, ZONES, EDGES, (1320, 850), "MomentLens notional architecture",
                 "Natural-language search for the right moment in a video, built on Azure AI services",
                 "Users reach an Azure App Service web app. Videos are stored in Azure Storage behind private endpoints and "
                 "analysed by Azure AI Video Indexer, with a Container Apps job collecting the insights. Azure OpenAI enriches "
                 "and embeds the scenes, and Azure AI Search serves hybrid, semantic and vector queries. A managed identity, a "
                 "virtual network, a container registry and Azure Monitor support every component.")
    OUT.write_text(svg, encoding="utf-8")
    print(f"wrote {OUT.relative_to(HERE.parent.parent)} ({len(svg) // 1024} KiB)")


if __name__ == "__main__":
    main()
