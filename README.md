# ksh_mon

Monitoring app for Kyle's PC, optimized for game-time reliability and early warning before lag/crash.

## v1 Monitoring Scope

This repository now defines a practical baseline of metrics and alerts across:

- CPU health
- GPU health
- Memory pressure
- Disk and storage health
- Network quality for gaming
- Process/game-specific stability
- OS/system integrity
- Power/hardware reliability
- Peripheral/input reliability
- Remote access/mesh monitoring
- Security baseline signals

## Prioritized v1 Signals (Top 10)

1. CPU sustained high load + high temperature
2. GPU thermal throttle or VRAM pressure
3. RAM pressure with rising hard faults
4. Disk latency/queue saturation
5. Packet loss/jitter bursts to game endpoints
6. DNS failures/latency spikes
7. Game process crash/hang detection
8. Service restart loops / critical system events
9. VPN/mesh tunnel instability
10. USB/audio device flap storms

## Alerting Model

- Severity levels: `info`, `warn`, `critical`
- Dedup windows to suppress repeated identical alerts
- Cooldowns per rule to reduce alert spam
- Compound alerts that combine multiple weak signals into high-confidence incidents

## Configuration Files

- `/home/runner/work/ksh_mon/ksh_mon/config/monitors.v1.yaml`
  - Metric definitions, thresholds, durations, severities, cooldown/dedup, and compound alerts
- `/home/runner/work/ksh_mon/ksh_mon/config/endpoints.example.yaml`
  - Example game, DNS, VPN, and remote monitoring targets

These files are intended as the initial implementation contract for the monitoring engine.
