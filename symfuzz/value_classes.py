"""
value_classes.py — Classify an input-port value into one of nine semantic
classes. Signatures are built as byte strings of class indices, one per
data-input port in a canonical order.
"""
from __future__ import annotations


# Class indices — keep stable; stored in sqlite value_class_hits.signature.
VC_ZERO   = 0
VC_ONE    = 1
VC_SMALL  = 2   # 2..15
VC_MSB    = 3   # exactly MSB set
VC_NEG1   = 4   # all-ones
VC_MAX    = 5   # MSB clear, rest set  (INT_MAX)
VC_POW2   = 6   # exactly one bit set, not MSB, not bit0
VC_ALT    = 7   # 0xAA…AA / 0x55…55
VC_MID    = 8   # fallback

CLASS_LABELS = [
    "zero", "one", "small", "msb", "neg1",
    "max",  "pow2", "alt",   "mid",
]


def classify(val: int, width: int) -> int:
    if width <= 0:
        return VC_ZERO
    mask = (1 << width) - 1
    v = val & mask
    if v == 0:
        return VC_ZERO
    if v == 1:
        return VC_ONE
    if 2 <= v <= 15:
        return VC_SMALL
    if width > 1 and v == (1 << (width - 1)):
        return VC_MSB
    if v == mask:
        return VC_NEG1
    if width > 1 and v == (mask ^ (1 << (width - 1))):
        return VC_MAX
    if v & (v - 1) == 0:           # single-bit set
        return VC_POW2
    aa = 0xAAAAAAAAAAAAAAAA & mask
    ss = 0x5555555555555555 & mask
    if v == aa or v == ss:
        return VC_ALT
    return VC_MID


def signature_for(inputs: dict, port_order: list) -> bytes:
    """Build a canonical-order signature from *inputs* over *port_order*
    (a list of PortInfo with .name, .width). Returns a compact bytes."""
    out = bytearray(len(port_order))
    for i, p in enumerate(port_order):
        out[i] = classify(int(inputs.get(p.name, 0)), p.width)
    return bytes(out)


def representative_value(cls_idx: int, width: int, rng=None) -> int:
    """Return an integer of width *width* whose class is *cls_idx*.
    When *rng* is provided, class buckets with multiple representatives
    draw a random pick (so repeated calls with the same class
    diversify concrete values)."""
    import random as _r
    r = rng if rng is not None else _r.Random()
    mask = (1 << width) - 1
    msb  = 1 << (width - 1) if width > 0 else 0
    if cls_idx == VC_ZERO:  return 0
    if cls_idx == VC_ONE:   return 1 & mask
    if cls_idx == VC_SMALL: return r.randint(2, 15) & mask if width >= 4 else (2 & mask)
    if cls_idx == VC_MSB:   return msb
    if cls_idx == VC_NEG1:  return mask
    if cls_idx == VC_MAX:   return mask ^ msb
    if cls_idx == VC_POW2:
        if width <= 1: return 0
        bit = r.randint(0, width - 2)  # exclude MSB
        return (1 << bit) & mask
    if cls_idx == VC_ALT:
        return r.choice([0xAAAAAAAAAAAAAAAA & mask,
                         0x5555555555555555 & mask])
    # VC_MID fallback: any value not matching the other classes.
    for _ in range(8):
        v = r.randint(0, mask)
        if classify(v, width) == VC_MID:
            return v
    return r.randint(0, mask)


def inputs_for_signature(sig: bytes, port_order: list, rng=None) -> dict:
    """Build an input dict whose value-classes match *sig* port-for-port."""
    out: dict = {}
    for i, p in enumerate(port_order):
        cls = sig[i] if i < len(sig) else VC_MID
        out[p.name] = representative_value(cls, p.width, rng)
    return out


def missing_signatures(seen: list[bytes], n_ports: int,
                       limit: int = 8) -> list[bytes]:
    """Return up to *limit* signatures not in *seen*. Strategy: flip one
    class index at a time relative to a seen signature — cheap, close to
    reachable space. Falls back to random signatures if seen is empty."""
    import random as _r
    seen_set = set(seen)
    out: list[bytes] = []
    if not seen:
        # Seed 1 random starting signature if nothing's recorded yet.
        seeds = [bytes(_r.randint(0, 8) for _ in range(n_ports))]
    else:
        seeds = list(seen_set)
    for s in seeds:
        arr = list(s)
        for i in range(n_ports):
            for cls in range(9):
                if cls == (arr[i] if i < len(arr) else VC_MID):
                    continue
                trial = list(arr)
                while len(trial) < n_ports:
                    trial.append(VC_MID)
                trial[i] = cls
                cand = bytes(trial)
                if cand not in seen_set:
                    out.append(cand)
                    seen_set.add(cand)
                    if len(out) >= limit:
                        return out
    return out


def decode_signature(sig: bytes, port_order: list) -> dict[str, str]:
    """Render a signature back to a {port_name: class_label} dict."""
    result: dict[str, str] = {}
    for i, p in enumerate(port_order):
        if i < len(sig):
            idx = sig[i]
            result[p.name] = CLASS_LABELS[idx] if idx < len(CLASS_LABELS) else "?"
    return result
