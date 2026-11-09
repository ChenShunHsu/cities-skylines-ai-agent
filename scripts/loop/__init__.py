"""An autonomous play loop for any Cities: Skylines map, built on the typed mayor tools.

Each wake runs the simulation, reads the city, grows it, fixes its problems and traffic, and
stops only for what needs a planner. Every decision comes from game data through the tools;
what a planner decides about one city (planned land use, notes) and what the loop learns there
(which remedies work) is kept in that city's private profile (see loop.city).
"""
