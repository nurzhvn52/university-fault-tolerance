"""Greedy timetable scheduler (pure logic, no I/O).

Sections are placed from the largest to the smallest. Each one goes into the least used
timeslot that still has a free room big enough, and into the smallest such room.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class SectionDemand:
    section_id: int
    capacity: int


@dataclass(frozen=True)
class RoomInfo:
    room_id: int
    capacity: int


Placement = tuple[int, int]  # (room_id, timeslot_id)


def order_sections(sections: Iterable[SectionDemand]) -> list[SectionDemand]:
    return sorted(sections, key=lambda section: (-section.capacity, section.section_id))


class Scheduler:
    def __init__(self, rooms: Iterable[RoomInfo], timeslot_ids: Iterable[int]) -> None:
        self._rooms = sorted(rooms, key=lambda room: (room.capacity, room.room_id))
        self._timeslot_ids = list(timeslot_ids)
        self._occupied: set[Placement] = set()
        self._slot_load: Counter[int] = Counter()

    def reserve(self, room_id: int, timeslot_id: int) -> None:
        """Mark a room and timeslot as used (for placements made earlier)."""
        self._occupied.add((room_id, timeslot_id))
        self._slot_load[timeslot_id] += 1

    def place(self, section: SectionDemand) -> Placement | None:
        """Choose and reserve a room and timeslot, or return None if nothing fits."""
        for timeslot_id in sorted(self._timeslot_ids, key=lambda t: (self._slot_load[t], t)):
            for room in self._rooms:
                if room.capacity >= section.capacity and (
                    (room.room_id, timeslot_id) not in self._occupied
                ):
                    self.reserve(room.room_id, timeslot_id)
                    return room.room_id, timeslot_id
        return None
