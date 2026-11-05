^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
Changelog for package ros2_robot_dashboard
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

0.1.0 (2026-11-05)
------------------
First release.

* Qt-free core (``CoreRuntime``) with an event bus, futures, a worker pool and a
  single-threaded executor in its own rclpy context; compatibility layer for Humble,
  Jazzy and Lyrical.
* Pages: Parameters, Topics, Services / Actions, Graph, TF, Robot, Controllers,
  Diagnostics & logs, Launch, Bags, Health, Utilities (plot, parameter compare).
* Parameters: typed and range-aware editors, auto-apply with debounce, batch and atomic
  apply, detection of external changes, presets in ``ros2 param dump`` format, double
  confirmation of critical parameters.
* Topics: shared subscriptions, rate and bandwidth, QoS details and mismatch detection,
  sampled echo, echo to file.
* Launch manager: YAML profiles with typed arguments, ``enabled_if`` conditions, presets,
  import of ``--show-args``, scenarios, clean stop (SIGINT, SIGTERM, SIGKILL) with
  detection of leftover processes.
* ``ros2_control``: switch preview with claimed interfaces and conflicts, strict or
  best-effort switch, hardware components lifecycle.
* TF echo with quaternion, RPY (rad, deg) and SE(3) output; detection of TF problems.
* Health: critical-topic profiles, ``host_monitor`` node for CPU, memory and network of
  robot PCs, discovery checks.
* Safety: read-only mode, confirmations, action log.
* Global search (Ctrl+K), light and dark themes, PNG/SVG/DOT graph export.
* Workaround for the fontconfig cache crash in conda/pixi environments.
* Tests for core, ROS services and Qt pages (offscreen); end-to-end tour on a UR
  mock-hardware cell; GitHub Actions CI on Humble, Jazzy and Lyrical with pixi.
