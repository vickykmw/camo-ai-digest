"""
Style envelopes — one place to tune the visual system per pillar.

Each pillar gets a three-tone palette (midtone + highlight + shadow) and any
pillar-specific instruction overrides. The base envelope is shared.

Editing this file changes every future generation. Edit with care.
"""

PILLAR_ENVELOPES = {
    "AI Adoption": {
        "midtone": "#3E5C76",      # steel blue
        "highlight": "#EACDCA",    # warm pale pink — temperature contrast on cool midtone
        "shadow": "#1A2732",       # darkened steel blue
    },
    "AI & Incentives": {
        "midtone": "#9A7D3F",      # brass
        "highlight": "#F4EAD5",    # soft cream
        "shadow": "#2A2008",       # darkened brass
    },
    "AI & Jobs": {
        "midtone": "#977C6E",      # clay-taupe
        "highlight": "#F0E0D2",    # bone
        "shadow": "#2F211B",       # darkened clay
    },
    "AI Algorithms & Data": {
        "midtone": "#4A4467",      # slate-violet
        "highlight": "#D8CFE6",    # pale lilac
        "shadow": "#1A1626",       # darkened slate-violet
    },
}


STYLE_ENVELOPE_TEMPLATE = """{visual_concept}

The image is a minimalist pointillist stipple illustration: the entire image \
is constructed from small dots, varying in density, size, and color. The \
subject is formed where dots cluster densely with rich saturated color, \
giving the subject volume and presence. Around and behind the subject, the \
dots scatter and thin out into soft pastel mists and color bleeds — creating \
organic, watercolor-like atmospheric gradients that suggest space and depth \
without any literal background detail. Where dots are densely packed, the \
color is rich and saturated; where they scatter and reveal the field below, \
it creates highlights and soft fades.

Three-color palette only:
- Highlight {highlight}: brightest highlights on the subject and atmospheric \
glow scattered around it
- Midtone {midtone}: midtones and the bulk of the subject's form
- Shadow {shadow}: deep shadows, the dark calm field at the base of the \
composition, and the negative space the dots scatter into

Dot density varies smoothly — dense and saturated where the subject lives, \
scattering into colored mist outward, eventually revealing the dark field. \
The subject is the clear visual focus; the surrounding atmosphere is ambient \
and soft, leaving the lower portion of the frame quieter for editorial text \
to be set over it.

Aesthetic references: Georges Seurat pointillism, vintage halftone newspaper \
portraits, Risograph art print of a single subject, screen-printed editorial \
illustration with stippled gradients, fine-art stipple drawing.

No text in image. No logos. No human faces. No hands.

Negative: NOT pixel art, NOT 8-bit graphics, NOT video game aesthetic, NOT \
flat vector illustration, NOT cartoon, NOT cell-shaded, NOT photorealistic \
photography, NOT smooth flat gradients, NOT graphic poster design with flat \
color blocks. No humanoid robot, no glowing brain, no handshakes, no \
lightbulbs, no binary code streams, no circuit board patterns, no generic \
corporate stock photography, no blue neon glow, no sci-fi HUD, no chrome 3D \
render, no hexagon tech grids, no Matrix-style code rain."""


def synthesize_prompt(visual_concept: str, pillar: str) -> str:
    """Wrap a per-paper visual concept in the pillar's style envelope."""
    if pillar not in PILLAR_ENVELOPES:
        raise ValueError(f"Unknown pillar: {pillar}")
    env = PILLAR_ENVELOPES[pillar]
    return STYLE_ENVELOPE_TEMPLATE.format(
        visual_concept=visual_concept,
        highlight=env["highlight"],
        midtone=env["midtone"],
        shadow=env["shadow"],
    )
