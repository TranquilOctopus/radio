from __future__ import annotations

from dataclasses import dataclass

from pkm.config import ChunkerConfig
from pkm.transcribe.base import Transcript


@dataclass(slots=True)
class Chunk:
    index: int
    text: str
    start: float
    end: float
    n_words: int


def chunk_transcript(transcript: Transcript, config: ChunkerConfig) -> list[Chunk]:
    segments = transcript.segments
    if not segments:
        return []

    chunks: list[Chunk] = []
    chunk_index = 0

    # seg_start_idx is the index into segments where the current chunk begins.
    seg_start_idx = 0

    while seg_start_idx < len(segments):
        chunk_segs = []
        chunk_start = segments[seg_start_idx].start

        # Accumulate until the window exceeds target_seconds.
        i = seg_start_idx
        while i < len(segments):
            seg = segments[i]
            chunk_segs.append(seg)
            elapsed = seg.end - chunk_start
            if elapsed >= config.target_seconds:
                break
            i += 1

        chunk_end = chunk_segs[-1].end
        text = " ".join(s.text.strip() for s in chunk_segs)
        n_words = sum(len(s.text.split()) for s in chunk_segs)

        chunks.append(
            Chunk(
                index=chunk_index,
                text=text,
                start=chunk_start,
                end=chunk_end,
                n_words=n_words,
            )
        )
        chunk_index += 1

        # If we consumed all remaining segments, we're done.
        if chunk_segs[-1] is segments[-1]:
            break

        # Carry back overlap_seconds worth of segments into the next chunk.
        # Walk backwards from the last segment until we've covered overlap_seconds.
        overlap_start = chunk_end - config.overlap_seconds
        new_start_idx = len(segments) - 1
        # The segment after the last consumed one is i+1; walk back from i.
        for j in range(i, seg_start_idx, -1):
            if segments[j].start <= overlap_start:
                new_start_idx = j + 1
                break
        else:
            # All segments from seg_start_idx onward are within the overlap window;
            # advance by at least one to guarantee progress.
            new_start_idx = seg_start_idx + 1

        # Guard: never go backwards or stay still (infinite-loop protection).
        seg_start_idx = max(new_start_idx, seg_start_idx + 1)

    return chunks
