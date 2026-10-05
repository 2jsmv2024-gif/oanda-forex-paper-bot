# OANDA 9-Strategy Architecture

Signal families:
1. A
2. B
3. V2
4. V3 NORMAL
5. V3 OPPOSITE
6. 57-59 NORMAL
7. 57-59 OPPOSITE
8. 57-59 TRUE REVERSE

Portfolio layer:
9. MFP FROZEN

A and B are aliases of the locked official research streams:
- A: 57-59 OPPOSITE execution stream.
- B: mathematical TRUE REVERSE of the completed A/57-59 OPPOSITE stream.

The frozen MFP source is not modified. It remains a separate portfolio-selection/execution layer.

Safety defaults:
- OANDA Practice endpoint.
- OANDA_LIVE_TRADING=false.
- Monitoring may cover all signal families.
- Broker order execution remains disabled until explicitly enabled.
