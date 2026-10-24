"""Human-like pacing and input for official-page automation.

Delays are bounded log-normal samples per action kind; ``PACE = 0`` disables
every wait (used by the offline regression suites). Input paths use only
trusted CDP keyboard events, never JS value assignment. ``wander`` touches
nothing: mouse moves and hovers only, never clicks, uploads or any other
server-state change.
"""
from __future__ import annotations
import asyncio
import math
import random

# Test seam: 0 disables all waits and mouse wander; input still runs.
PACE = 1.0

# kind -> (median seconds, log-normal sigma, lower bound, upper bound)
KINDS = {
    'micro': (0.15, 0.55, 0.05, 0.5),
    'action': (0.4, 0.5, 0.15, 1.2),
    'read': (1.2, 0.6, 0.5, 3.0),
    'transition': (0.4, 0.5, 0.2, 1.1),
    'pre_submit': (2.0, 0.5, 0.8, 5.0),
}


def delay(kind: str) -> float:
    median, sigma, low, high = KINDS[kind]
    value = random.lognormvariate(math.log(median), sigma)
    return max(low, min(high, value)) * PACE


async def pause(kind: str = 'action') -> None:
    seconds = delay(kind)
    if seconds > 0:
        await asyncio.sleep(seconds)


async def wander(page, moves: int | None = None) -> None:
    """Idle pointer motion only. Never clicks, types or changes page state."""
    if PACE <= 0:
        return
    try:
        viewport = page.viewport_size or {'width': 1280, 'height': 900}
    except Exception:
        viewport = {'width': 1280, 'height': 900}
    count = moves if moves is not None else random.randint(2, 4)
    for _ in range(count):
        try:
            x = random.randint(max(1, int(viewport['width'] * .1)),
                               max(2, int(viewport['width'] * .9)))
            y = random.randint(max(1, int(viewport['height'] * .15)),
                               max(2, int(viewport['height'] * .85)))
            await page.mouse.move(x, y, steps=random.randint(8, 20))
            await pause('micro')
        except Exception:
            return


async def enter_prompt(page, textbox, prompt: str) -> None:
    """Enter the prompt like a person: click, clear, paste the bulk, type the tail.

    Single-line prompts are pasted up to the last few characters and the tail is
    typed character by character. Prompts containing newlines are inserted in one
    trusted ``insert_text`` call: pressing Enter as a key event could submit the
    composer instead of writing a newline. Callers still verify the final text
    via ``editor_prompt`` and raise ``prompt_unconfirmed`` on any mismatch.
    """
    await textbox.click()
    await pause('micro')
    keyboard = page.keyboard
    await keyboard.press('ControlOrMeta+a')
    await keyboard.press('Delete')
    await pause('micro')
    if not prompt:
        return
    if '\n' in prompt:
        head, tail = prompt, ''
    elif len(prompt) <= 2:
        head, tail = prompt, ''
    else:
        split = random.randint(len(prompt) - min(5, len(prompt) - 1), len(prompt) - 1)
        head, tail = prompt[:split], prompt[split:]
    if head:
        await keyboard.insert_text(head)
        await pause('micro')
    for char in tail:
        await keyboard.type(char)
        await asyncio.sleep(delay('micro'))
