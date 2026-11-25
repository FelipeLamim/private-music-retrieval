"""Playback model used to derive startup delay and stalls from measured block timelines.

Input: for each decoded block, the time (s since the user's click) at which its PCM became
available to the player, and its PCM length. Model: a player that starts when ``start_s``
seconds of audio are buffered (or the whole track is available), plays in real time, and
when the buffer runs dry stops ("stall") until ``start_s`` seconds are buffered again (or the
rest of the track is available). This is a model applied to measured arrival times, not a
measurement of a real audio device; the browser test measures a real player separately.
"""
from __future__ import annotations

from typing import Iterable


def simulate(arrivals: Iterable[tuple[float, int]], total_pcm: int, *, bytes_per_s: int = 32000,
             start_s: float = 1.0) -> dict[str, float]:
    """``arrivals``: (time_available_s, pcm_bytes) per block, in playback order."""
    times: list[float] = []
    cum: list[int] = []
    acc = 0
    for t, n in arrivals:
        if n <= 0:
            continue
        acc += n
        times.append(t)
        cum.append(acc)
    if not times or total_pcm <= 0:
        return {"startup_s": float("nan"), "stalls": 0, "stall_s": 0.0, "end_s": float("nan")}
    dur = total_pcm / bytes_per_s
    need = min(start_s * bytes_per_s, total_pcm)

    def time_when_available(audio_pos_bytes: float) -> float:
        # earliest arrival time at which cum >= audio_pos_bytes
        lo, hi = 0, len(cum) - 1
        if audio_pos_bytes > cum[-1]:
            return float("inf")
        while lo < hi:
            mid = (lo + hi) // 2
            if cum[mid] >= audio_pos_bytes:
                hi = mid
            else:
                lo = mid + 1
        return times[lo]

    clock = time_when_available(need)
    startup = clock
    pos = 0.0  # seconds of audio played
    stalls, stall_s = 0, 0.0
    while pos < dur - 1e-9:
        # play until the next point where data runs out
        avail_bytes = 0
        # bytes available at current clock
        idx = 0
        lo, hi = 0, len(times)
        while lo < hi:
            mid = (lo + hi) // 2
            if times[mid] <= clock:
                lo = mid + 1
            else:
                hi = mid
        idx = lo - 1
        avail_bytes = cum[idx] if idx >= 0 else 0
        avail_s = avail_bytes / bytes_per_s
        if avail_s >= dur - 1e-9:
            clock += dur - pos
            pos = dur
            break
        if avail_s > pos + 1e-9:
            # play buffered audio; more may arrive meanwhile
            nxt = idx + 1
            play_until = avail_s
            # advance through arrivals that happen before buffer is exhausted
            t_exhaust = clock + (play_until - pos)
            while nxt < len(times) and times[nxt] < t_exhaust:
                play_until = cum[nxt] / bytes_per_s
                t_exhaust = clock + (play_until - pos)
                nxt += 1
            clock = t_exhaust
            pos = min(play_until, dur)
            continue
        # buffer empty: stall until start_s more seconds (or the rest) are available
        stalls += 1
        target = min(pos * bytes_per_s + need, total_pcm)
        t_resume = time_when_available(target)
        stall_s += t_resume - clock
        clock = t_resume
    return {"startup_s": startup, "stalls": stalls, "stall_s": stall_s, "end_s": clock}
