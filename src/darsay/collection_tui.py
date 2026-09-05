"""The terminal collection room. Standard-library curses; no payload writes.

The state machine is independent of terminal drawing. Inventory names are
untrusted text, never terminal instructions. A chosen include union is
returned only after review; cancelling leaves no archive to resume.
"""

from __future__ import annotations

import contextlib
import curses
import os
import textwrap
import unicodedata
from dataclasses import dataclass, field

from .collection import GUIDE, family, publication, selection_totals, starting_selection
from .readme_gen import human_size


def safe_text(value: object) -> str:
    """Strip control sequences, bidi overrides, and other non-printing controls."""
    return "".join(c if c.isprintable() else " " for c in str(value))


def clipped(value: object, width: int) -> str:
    out, used = [], 0
    for char in safe_text(value):
        cells = (
            0
            if unicodedata.combining(char)
            else 2
            if unicodedata.east_asian_width(char) in {"W", "F"}
            else 1
        )
        if used + cells > width:
            break
        out.append(char)
        used += cells
    return "".join(out)


@dataclass
class CollectionState:
    inventory: dict
    include: list[str] = field(default_factory=list)
    cursor: int = 0
    offset: int = 0
    page: str = "choose"
    intent: str | None = None
    message: str = "Choose a starting point, or select variants yourself."

    @property
    def groups(self) -> list[dict]:
        return self.inventory["variants"] + self.inventory["companions"]

    def selected(self, variant: dict) -> bool:
        return "/*" in self.include or all(
            p in self.include for p in variant["include"]
        )

    def key(self, key: str) -> str | None:
        """Return confirm/cancel only on a terminal decision; other keys edit a draft."""
        if key in {"q", "Q", "\x03"}:
            return "cancel"
        if key == "\x1b":
            if self.page == "choose":
                return "cancel"
            self.page, self.offset = "choose", 0
            return None
        if key in {"KEY_UP", "k", "KEY_DOWN", "j", "KEY_NPAGE", "KEY_PPAGE"}:
            step = -1 if key in {"KEY_UP", "k", "KEY_PPAGE"} else 1
            if key in {"KEY_NPAGE", "KEY_PPAGE"}:
                step *= 8
            if self.page == "choose":
                self.cursor = max(0, min(len(self.groups) - 1, self.cursor + step))
            else:
                self.offset = max(0, self.offset + step)
            return None
        if key == "?":
            self.page, self.offset = ("choose" if self.page == "guide" else "guide"), 0
        elif self.page == "choose" and key in {"1", "2", "3"}:
            self.intent = {"1": "single", "2": "compare", "3": "whole"}[key]
            self.include = starting_selection(self.inventory["variants"], self.intent)
            count = sum(self.selected(v) for v in self.inventory["variants"])
            wanted = 2 if key == "2" else 1
            self.message = GUIDE["intents"][self.intent]["note"]
            if key != "3":
                self.message = (
                    "Smallest complete, known-size 4-bit"
                    + (" + 8-bit" if key == "2" else "")
                    + " starting point. "
                    + self.message
                )
                if count < wanted:
                    self.message = (
                        "A requested family is unavailable. Select variants manually."
                    )
            self.cursor = next(
                (i for i, v in enumerate(self.groups) if self.selected(v)), 0
            )
        elif self.page == "choose" and key == " " and self.groups:
            variant = self.groups[self.cursor]
            if not variant["complete"]:
                self.message = "Incomplete shard group: cannot select as a complete variant. Whole publication retains it as found."
                return None
            chosen = [
                v
                for v in self.groups
                if v["complete"] and self.selected(v) and v is not variant
            ]
            if not self.selected(variant):
                chosen.append(variant)
            self.include = sorted({p for v in chosen for p in v["include"]})
            self.intent = None
            self.message = "Your selection. Support files accompany it once; other files stay outside the scope."
        elif key in {"\n", "\r", "KEY_ENTER"}:
            if self.page == "review":
                return "confirm"
            if self.page == "choose" and self.include:
                self.page, self.offset = "review", 0
            elif self.page == "choose":
                self.message = "Choose at least one variant or the whole publication before review."
        return None

    def field_notes(self) -> list[str]:
        variant = self.groups[self.cursor] if self.groups else None
        text = GUIDE["families"][family(variant["precision"] if variant else None)]
        companion = variant in self.inventory["companions"] if variant else False
        return [
            "A SMALL FIELD GUIDE",
            "",
            safe_text(variant["precision"] or variant["name"]) if variant else "GGUF",
            safe_text(variant["name"]) if variant else "",
            "A companion, not a copy." if companion else text["label"],
            "",
            GUIDE["companions"] if companion else text["meaning"],
            "",
            "WHY KEEP THIS?",
            GUIDE["companion_collect"] if companion else text["collect"],
            "",
            GUIDE["recovery"]["label"].upper(),
            GUIDE["recovery"]["description"],
            "",
            GUIDE["recovery"]["note"],
        ]

    def review_lines(self) -> list[str]:
        total = selection_totals(self.inventory["files"], self.include)
        chosen = [v for v in self.groups if self.selected(v)]
        companions = sum(v in self.inventory["companions"] for v in chosen)
        lines = [
            "A COLLECTION WITH INTENTION",
            "",
            (">= " if total["unknown"] else "")
            + human_size(total["bytes"])
            + " on disk",
            f"{len(chosen) - companions} model variants / {companions} companions / {total['files']} files",
            "",
        ]
        lines += [
            f"  {v['name']}  /  {human_size(v['size_bytes'])}"
            + (" / incomplete group, retained as found" if not v["complete"] else "")
            for v in chosen
        ]
        lines += [
            "",
            "Support files accompany the selection, counted once.",
            GUIDE["sizing"],
        ]
        if total["unknown"]:
            lines += [
                f"{total['unknown']} sizes unknown: the amount is a lower bound, not a storage budget."
            ]
        if self.inventory["companions"] and not companions:
            lines += [
                "No projector selected. Multimodal use may need a matching companion; consult the publisher and runtime."
            ]
        if len(chosen) - companions > 1:
            lines += [
                "Multiple variants form an archive collection, not one runnable model. Prepare a runtime with one encoding and matching companions."
            ]
        lines += ["", "PINNED REVISION", self.inventory["revision"], ""]
        if "/*" in self.include:
            lines += [
                "SCOPE",
                "The whole repository at this revision: no include selectors. What the archive retains inside it is a separate, recorded decision.",
            ]
        else:
            lines += ["EXACT INCLUDE SELECTORS", *self.include]
        lines += [
            "",
            GUIDE["recovery"]["label"],
            GUIDE["recovery"]["description"],
            "",
            "Unselected artifacts are outside the collection, not proven disposable.",
            "",
            "Enter confirms this scope and continues to the archive plan. Esc refines it. Q cancels.",
        ]
        return lines


def wrapped(lines: list[str], width: int) -> list[str]:
    out = []
    for line in lines:
        for part in textwrap.wrap(safe_text(line), max(2, width)) or [""]:
            if not part:
                out.append("")
            while part:
                # textwrap counts codepoints, not terminal cells. Keep every
                # character of a wide filename by wrapping it further.
                piece = clipped(part, max(2, width))
                out.append(piece)
                part = part[len(piece) :]
    return out


def cells(text: str) -> int:
    """Terminal columns a string occupies: wide East Asian glyphs take two."""
    return sum(
        0
        if unicodedata.combining(c)
        else 2
        if unicodedata.east_asian_width(c) in {"W", "F"}
        else 1
        for c in text
    )


# Row budget for the choose page. Rows 1-4 are the masthead, 5-7 the question
# and starting points, the list runs from LIST_TOP, and the last seven rows
# hold the rule, a two-row message, the total, the meter, and the keys.
LIST_TOP = 8
BOTTOM_ROWS = 8


def _room(screen, state: CollectionState) -> list[str]:
    with contextlib.suppress(curses.error):
        curses.curs_set(0)
    screen.keypad(True)
    colors = {
        "gold": curses.A_BOLD,
        "muted": curses.A_NORMAL,
        "focus": curses.A_REVERSE,
        "plain": curses.A_NORMAL,
    }
    with contextlib.suppress(curses.error):
        if curses.has_colors() and "NO_COLOR" not in os.environ:
            curses.start_color()
            curses.use_default_colors()
            rich = curses.COLORS >= 256
            for pair, fg, bg in [
                (1, 179 if rich else curses.COLOR_YELLOW, -1),
                (2, 246 if rich else curses.COLOR_WHITE, -1),
                (
                    3,
                    223 if rich else curses.COLOR_YELLOW,
                    235 if rich else curses.COLOR_BLACK,
                ),
            ]:
                curses.init_pair(pair, fg, bg)
            colors.update(
                gold=curses.color_pair(1) | curses.A_BOLD,
                muted=curses.color_pair(2),
                focus=curses.color_pair(3) | curses.A_BOLD,
            )

    def put(y, x, text, style="plain", width=None):
        height, columns = screen.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= columns - 1:
            return
        with contextlib.suppress(curses.error):
            screen.addstr(
                y,
                x,
                clipped(
                    text, min(columns - x - 1, width if width is not None else columns)
                ),
                colors[style],
            )

    def masthead(height, width):
        put(1, 2, "darsay / THE COLLECTION ROOM", "gold")
        # The step being taken is lit; the other waits in the margin.
        x = max(33, width - 26)
        for i, (label, current) in enumerate(
            [
                ("01 choose", state.page != "review"),
                ("02 review", state.page == "review"),
            ]
        ):
            put(1, x, label, "gold" if current else "muted")
            x += len(label)
            if i == 0:
                put(1, x, "  /  ", "muted")
                x += 5
        put(2, 2, state.inventory["source"], "gold")
        put(
            3,
            2,
            "Revision " + state.inventory["revision"] + " / metadata only",
            "muted",
        )
        put(4, 2, "─" * (width - 4), "muted")

    def reading_page(height, width):
        lines = wrapped(
            state.field_notes() if state.page == "guide" else state.review_lines(),
            width - 6,
        )
        count = height - 10
        state.offset = min(state.offset, max(0, len(lines) - count))
        for i, line in enumerate(lines[state.offset : state.offset + count]):
            put(6 + i, 3, line, "gold" if line.isupper() else "plain")
        put(
            height - 3,
            2,
            f"Lines {state.offset + 1}-{min(len(lines), state.offset + count)} of {len(lines)} / up-down to scroll",
            "muted",
        )
        put(
            height - 2,
            2,
            "Enter: confirm scope & continue   Esc: refine   Q: cancel"
            if state.page == "review"
            else "Up/down: read   Esc: back   Q: cancel",
            "gold",
        )

    def choose_page(height, width):
        spacious = width >= 108
        divider = width - 40 if spacious else width - 2
        list_width = divider - 3
        put(5, 2, "What would you like to keep?", "gold")
        put(
            6,
            2,
            "1  One considered copy   2  A comparison pair   3  The whole publication"
            if list_width >= 72
            else "1  One copy   2  A pair   3  The whole publication",
            "gold",
            list_width,
        )
        put(
            7,
            2,
            "smallest 4-bit  /  4-bit + 8-bit  /  the repository as published",
            "muted",
            list_width,
        )
        rows = max(1, height - LIST_TOP - BOTTOM_ROWS)
        state.offset = max(0, min(state.offset, state.cursor))
        if state.cursor >= state.offset + rows:
            state.offset = state.cursor - rows + 1
        room = max(5, list_width - 27)
        for i, v in enumerate(state.groups[state.offset : state.offset + rows]):
            index = state.offset + i
            selected = state.selected(v)
            marker = "[!]" if not v["complete"] else "[x]" if selected else "[ ]"
            companion = v in state.inventory["companions"]
            # The full path stays on screen; identity is never collapsed.
            label = ("companion / " if companion else "") + (
                v["precision"] or v["name"]
            )
            label += " / " + v["name"]
            left = clipped(label, room)
            left += " " * max(0, room - cells(left))
            right = f"{human_size(v['size_bytes']):>10} {v['file_count']:>3}f"
            put(
                LIST_TOP + i,
                2,
                f"{'›' if index == state.cursor else ' '} {marker} {left} {right}",
                "focus" if index == state.cursor else "gold" if selected else "plain",
                list_width,
            )
        below = len(state.groups) - (state.offset + rows)
        status = f"{state.cursor + 1}/{len(state.groups)}  ·  Space keeps a complete group  ·  ? field guide"
        if state.offset:
            status += f"  ·  {state.offset} above"
        if below > 0:
            status += f"  ·  {below} more below"
        put(LIST_TOP + rows, 2, status, "muted", list_width)
        if spacious:
            for y in range(5, height - BOTTOM_ROWS + 1):
                put(y, divider, "│", "muted")
            notes = wrapped(state.field_notes(), 33)
            available = height - BOTTOM_ROWS - 5
            shown = notes[:available]
            if len(notes) > available and shown:
                shown[-1] = "… ? opens the full guide"
            for i, line in enumerate(shown):
                put(5 + i, divider + 3, line, "gold" if line.isupper() else "muted", 33)
        total = selection_totals(state.inventory["files"], state.include)
        whole = selection_totals(state.inventory["files"], ["/*"])
        amount = (">= " if total["unknown"] else "") + human_size(total["bytes"])
        put(height - 7, 2, "─" * (width - 4), "muted")
        for i, line in enumerate(wrapped([state.message], width - 4)[:2]):
            put(height - 6 + i, 2, line, "muted")
        put(
            height - 4,
            2,
            f"YOUR COLLECTION  {amount} / {total['files']} files"
            + (f" / {total['unknown']} sizes unknown" if total["unknown"] else ""),
            "gold",
        )
        span = max(5, min(28, width - 36))
        fraction = min(1, total["bytes"] / whole["bytes"]) if whole["bytes"] else 0
        filled = int(span * fraction)
        put(
            height - 3,
            2,
            "━" * filled
            + "·" * (span - filled)
            + "  of "
            + human_size(whole["bytes"])
            + (" known bytes" if whole["unknown"] else " publication"),
            "muted",
        )
        put(
            height - 2,
            2,
            "↑↓ move  Space select  1/2/3 start  Enter review  ? learn  Q cancel"
            if width >= 72
            else "↑↓ move · Space · 1/2/3 · Enter · ? · Q cancel",
            "gold",
        )

    while True:
        height, width = screen.getmaxyx()
        screen.erase()
        if height < 20 or width < 60:
            put(1, 1, "darsay / the collection room", "gold")
            put(3, 1, "Resize to at least 60 columns x 20 rows.")
            put(5, 1, "Q or Esc cancels. No archive has started.", "muted")
            screen.refresh()
            key = screen.getkey()
            if key in {"q", "Q", "\x1b", "\x03"}:
                raise KeyboardInterrupt
            continue
        masthead(height, width)
        if state.page == "choose":
            choose_page(height, width)
        else:
            reading_page(height, width)
        screen.refresh()
        result = state.key(screen.getkey())
        if result == "cancel":
            raise KeyboardInterrupt
        if result == "confirm":
            return state.include


def opening_state(inventory: dict, pinned: dict | None) -> CollectionState:
    """The room's first draft: empty, or the pin a forced re-pin would replace."""
    state = CollectionState(inventory)
    if pinned is None:
        return state
    state.include = list(pinned["include"] or ["/*"])
    state.cursor = next((i for i, v in enumerate(state.groups) if state.selected(v)), 0)
    state.message = (
        f"Pinned on disk: {pinned['verified']} verified files, "
        f"{human_size(pinned['verified_bytes'])}. Enter keeps this scope; "
        "a different choice removes what falls outside it."
    )
    return state


def choose_collection(snapshot, pinned: dict | None = None) -> list[str] | None:
    """Fresh multi-variant models only. Curses restores terminal modes on every exit.

    ``pinned`` is the scope a forced re-pin would replace; the room opens with
    it selected and says what a different choice removes.
    """
    inventory = publication(snapshot)
    if len(inventory["variants"]) < 2:
        return None
    try:
        return curses.wrapper(_room, opening_state(inventory, pinned))
    except curses.error as exc:
        raise SystemExit(
            "Could not open the collection picker. No archive has started. "
            "Use --include for an explicit selection, --full for every file, "
            "or --yes for the default archive policy."
        ) from exc
