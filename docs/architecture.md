# Architecture and development

## Layers

```
ros2_robot_dashboard/
├── core/                 no Qt imports: usable from scripts, tests or another GUI
│   ├── domain/           frozen dataclasses (params, graph, tf, control, process, …) and events
│   ├── compat/           per-distro differences (humble, jazzy, lyrical)
│   ├── msgs/             message introspection and YAML formatting
│   ├── processes/        process runner (process groups, clean stop, leftovers), launch args
│   ├── services/         one service per feature: params, topics, interfaces, graph, tf,
│   │                     robot_state, controllers, diagnostics, logs, lifecycle,
│   │                     launch, bags, health, host_metrics, utility
│   ├── runtime.py        CoreRuntime: rclpy context, node, executor thread, worker pool
│   ├── event_bus.py      thread-safe publish/subscribe of domain events
│   ├── futures.py        small Future used by every asynchronous call
│   ├── safety.py         read-only mode, confirmations, action log
│   └── config.py         defaults + YAML layers
├── adapters/qt/          PySide6 user interface
│   ├── qt_bridge.py      events → queued Qt signals; Future → slot on the GUI thread
│   ├── main_window.py    page list, lazy page creation, status bar, menus, search
│   ├── pages/            one page per feature (BasePage: enter/leave, read-only)
│   ├── models/, widgets/ table/tree models, parameter editors, message form, plot
│   └── env_fix.py        fontconfig workaround for conda/pixi
├── cli.py, main.py       command line → Qt application
└── host_monitor.py       publishes CPU/memory/network on /<host>/host_metrics
```

### Threads

- **Executor thread**: a `SingleThreadedExecutor` spins the dashboard node in its own
  rclpy `Context`, so it never interferes with other rclpy code in the same process.
  Subscription and timer callbacks only store data and publish events.
- **Worker pool**: anything that blocks (service calls with timeouts, `ros2 bag info`,
  Graphviz layouts, files) runs here. Results come back as `Future`s.
- **GUI thread**: pages never call ROS directly. They call core services, which return
  futures, and the `QtBridge` delivers results and bus events to the GUI thread through
  queued signals.

A single-threaded executor may seem a strange choice for a GUI backend, but in Python
it is much faster than `MultiThreadedExecutor`. Measured on a UR mock cell publishing
`/joint_states` at 500 Hz, the single-threaded executor received about 480 msg/s; the
multi-threaded one received 10 to 25 msg/s and starved the GUI. Because nothing blocks
in callbacks, one executor thread is enough. `core.executor: multi` is kept for
experiments.

### Shared subscriptions

Several pages often watch the same topic (`/joint_states` for Robot, Plot and Health).
`TopicService.acquire()` keeps one subscription per topic and QoS, and fans messages out
to consumers. The subscription is destroyed when the last consumer releases it. Pages
acquire their resources in `enter()` and release them in `leave()`, so hidden pages cost
nothing.

### Distro differences

`core/compat` isolates what changes between distros: endpoint and QoS information,
the messages of `ros2_control` (controllers, hardware interfaces and components,
`SwitchController` request and result), the `ros2 bag record/play/info` command lines,
and message-to-YAML formatting. `test/compat/test_compat_contract.py` checks that every
distro module implements the same interface. Launch arguments that differ between
distros, such as `use_fake_hardware` on Humble and `use_mock_hardware` later, are handled
in the launch profiles.

## Tests

```bash
pixi run -e jazzy test        # in this repository (no colcon build needed)
# or inside a colcon/pixi workspace:
python -m pytest -p no:launch_testing -p no:launch_ros test -q
```

| Folder | Content |
|---|---|
| `test/core/test_domain.py` etc. | pure logic: domain objects, presets, launch args, event bus |
| `test/core/test_process_runner.py` | process groups, stop escalation, leftovers |
| `test/core/test_services_ros.py` | every core service against dummy nodes (`test/fixtures/dummy_nodes.py`) |
| `test/compat/` | per-distro compatibility contract |
| `test/adapters_qt/test_pages.py` | every page opened and exercised offscreen with pytest-qt |

ROS tests use a random `ROS_DOMAIN_ID` and localhost-only discovery. They are skipped
when rclpy cannot start.

### End-to-end check with a UR cell

`tools/ur_cell_tour.py` starts the UR mock-hardware cell **from the Launch page**. It then
visits every page and checks real data: parameters, the 500 Hz rate, TF, joints, a
controller switch, a `FollowJointTrajectory` goal and a clean stop. With `--shots DIR`
it saves the screenshots used in the documentation.

```bash
# in a workspace with ros-<distro>-ur installed
ROS_DOMAIN_ID=77 python tools/ur_cell_tour.py --shots docs/images
```

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request:

- `lint`: flake8 (pycodestyle and pyflakes, 100 columns).
- `test`: the full suite on Humble, Jazzy and Lyrical, each in its pixi environment,
  with Qt in offscreen mode.

`pixi.lock` pins the environments, so CI and local runs use the same packages.
