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


def first_fit(
    sections: Iterable[SectionDemand], rooms: Iterable[RoomInfo], timeslot_ids: Iterable[int]
) -> dict[int, Placement] | None:
    """Alternate algorithm of the recovery block: a deliberately simple, different method
    (earliest timeslot and first room that fits, no balancing). None if a section cannot be
    placed."""
    rooms = sorted(rooms, key=lambda room: (room.capacity, room.room_id))
    timeslot_ids = list(timeslot_ids)
    occupied: set[Placement] = set()
    placements: dict[int, Placement] = {}
    for section in order_sections(sections):
        placement = next(
            (
                (room.room_id, slot)
                for slot in timeslot_ids
                for room in rooms
                if room.capacity >= section.capacity and (room.room_id, slot) not in occupied
            ),
            None,
        )
        if placement is None:
            return None
        occupied.add(placement)
        placements[section.section_id] = placement
    return placements


def acceptance_test(
    placements: dict[int, Placement] | None,
    sections: list[SectionDemand],
    rooms: list[RoomInfo],
    timeslot_ids: list[int],
    *,
    balanced: bool,
) -> list[str]:
    """Problems of a timetable; an empty list means it is accepted. The primary result must
    also be balanced (no timeslot with more than ceil(n / slots) + 1 sections), because
    crowded timeslots cause schedule conflicts for students."""
    if placements is None:
        return ["not every section could be placed"]
    problems = []
    capacity = {room.room_id: room.capacity for room in rooms}
    missing = {s.section_id for s in sections} - set(placements)
    if missing:
        problems.append(f"{len(missing)} sections without a room")
    if len(set(placements.values())) != len(placements):
        problems.append("a room is booked twice in one timeslot")
    too_small = [
        s.section_id
        for s in sections
        if s.section_id in placements and capacity[placements[s.section_id][0]] < s.capacity
    ]
    if too_small:
        problems.append(f"{len(too_small)} sections in rooms that are too small")
    if balanced and placements:
        per_slot = Counter(slot for _, slot in placements.values())
        limit = -(-len(sections) // len(timeslot_ids)) + 1
        if max(per_slot.values()) > limit:
            problems.append(f"a timeslot has {max(per_slot.values())} sections (limit {limit})")
    return problems
