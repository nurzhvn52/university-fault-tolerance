from collections import Counter

from timetable.scheduler import (
    RoomInfo,
    Scheduler,
    SectionDemand,
    acceptance_test,
    first_fit,
    order_sections,
)

ROOMS = [RoomInfo(1, 30), RoomInfo(2, 60), RoomInfo(3, 120)]
SLOTS = [10, 11, 12, 13]


def test_every_section_gets_a_room_that_fits_without_double_booking():
    scheduler = Scheduler(ROOMS, SLOTS)
    sections = order_sections(SectionDemand(i, capacity) for i, capacity in enumerate([25] * 8, 1))
    capacity_of = {room.room_id: room.capacity for room in ROOMS}

    placements = [scheduler.place(section) for section in sections]

    assert None not in placements
    assert len(set(placements)) == len(placements)
    assert all(capacity_of[room_id] >= 25 for room_id, _ in placements)


def test_sections_are_spread_over_timeslots():
    scheduler = Scheduler(ROOMS, SLOTS)

    placements = [scheduler.place(SectionDemand(i, 20)) for i in range(1, 9)]

    assert Counter(timeslot for _, timeslot in placements) == {10: 2, 11: 2, 12: 2, 13: 2}


def test_smallest_fitting_room_is_used():
    scheduler = Scheduler(ROOMS, SLOTS)

    assert scheduler.place(SectionDemand(1, 50)) == (2, 10)


def test_returns_none_when_no_room_is_big_enough_or_free():
    scheduler = Scheduler([RoomInfo(1, 30)], [10])

    assert scheduler.place(SectionDemand(1, 40)) is None
    assert scheduler.place(SectionDemand(2, 30)) == (1, 10)
    assert scheduler.place(SectionDemand(3, 30)) is None


def test_reserved_placements_are_not_reused():
    scheduler = Scheduler([RoomInfo(1, 30)], [10, 11])
    scheduler.reserve(1, 10)

    assert scheduler.place(SectionDemand(1, 30)) == (1, 11)


def test_largest_sections_are_scheduled_first():
    ordered = order_sections([SectionDemand(1, 30), SectionDemand(2, 90), SectionDemand(3, 30)])

    assert [section.section_id for section in ordered] == [2, 1, 3]


def test_first_fit_uses_the_earliest_timeslot():
    sections = [SectionDemand(1, 20), SectionDemand(2, 20)]

    assert first_fit(sections, ROOMS, SLOTS) == {1: (1, 10), 2: (2, 10)}


def test_first_fit_gives_up_when_a_section_does_not_fit():
    assert first_fit([SectionDemand(1, 500)], ROOMS, SLOTS) is None


def test_acceptance_test_accepts_a_valid_balanced_timetable():
    sections = [SectionDemand(i, 20) for i in range(1, 9)]
    scheduler = Scheduler(ROOMS, SLOTS)
    placements = {s.section_id: scheduler.place(s) for s in sections}

    assert acceptance_test(placements, sections, ROOMS, SLOTS, balanced=True) == []


def test_acceptance_test_finds_every_kind_of_problem():
    sections = [SectionDemand(1, 20), SectionDemand(2, 100), SectionDemand(3, 20)]
    placements = {1: (1, 10), 2: (1, 10)}

    problems = acceptance_test(placements, sections, ROOMS, SLOTS, balanced=False)

    assert problems == [
        "1 sections without a room",
        "a room is booked twice in one timeslot",
        "1 sections in rooms that are too small",
    ]
    assert acceptance_test(None, sections, ROOMS, SLOTS, balanced=False)


def test_unbalanced_first_fit_result_fails_the_primary_acceptance_test():
    rooms = [RoomInfo(i, 30) for i in range(1, 11)]
    sections = [SectionDemand(i, 20) for i in range(1, 9)]
    placements = first_fit(sections, rooms, SLOTS)

    # All 8 sections land in timeslot 10: valid, but far from balanced (limit 3).
    assert acceptance_test(placements, sections, rooms, SLOTS, balanced=False) == []
    assert acceptance_test(placements, sections, rooms, SLOTS, balanced=True) == [
        "a timeslot has 8 sections (limit 3)"
    ]
