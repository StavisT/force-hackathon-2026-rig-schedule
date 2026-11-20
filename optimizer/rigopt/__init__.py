"""Rig schedule optimizer (spec v0.4).

Modules
-------
data      load a dataset folder (either unit set) into an internal USD/bbl/MMBtu model
economics value tables (NPV / EMV per completion month, discount factors)
decoder   turns per-rig task sequences into a dated schedule under all hard rules
search    initial solution + seeded simulated annealing over sequences
analysis  KPIs, explanations, weather, campaigns, discovery risk, Monte Carlo, diffs
report    self-contained HTML report
check     independent acceptance checker (spec §13)
"""
