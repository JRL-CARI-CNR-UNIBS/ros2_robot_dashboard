# Configuration

## Where files live

| What | Location |
|---|---|
| User settings (overrides) | `$ROS_HOME/robot_dashboard/dashboard.yaml` (`$ROS_HOME` defaults to `~/.ros`) |
| Launch profiles | `$ROS_HOME/robot_dashboard/launch_profiles/*.yaml` |
| Health profiles | `$ROS_HOME/robot_dashboard/health_profiles/*.yaml` |
| Parameter presets | `$ROS_HOME/robot_dashboard/param_presets/*.yaml` (default folder) |
| Action log, graph layout | `$XDG_STATE_HOME/ros2_robot_dashboard/` (`~/.local/state/...`) |
| Window layout, page options | Qt settings (`~/.config/ros2_robot_dashboard/ros2_robot_dashboard.conf`) |

Everything under `$ROS_HOME/robot_dashboard` is plain YAML. You can copy it between
machines or keep it in the robot's repository.

Settings are merged in this order, later ones winning:

1. Built-in defaults (`ros2_robot_dashboard/core/config.py`).
2. The installed `share/ros2_robot_dashboard/config/dashboard.yaml`.
3. `$ROS_HOME/robot_dashboard/dashboard.yaml`.
4. The file given with `--config`.
5. Command-line options (`--read-only`, `--distro`…).

Only write the keys you want to change.

## Main settings

```yaml
read_only: false            # start in read-only mode
confirm_actions: true       # ask before risky actions

timeouts:
  service_s: 2.0
  switch_controller_s: 5.0

params:
  auto_apply: true
  atomic_apply: false
  debounce_ms: 200
  hide_regex: '^(use_sim_time|qos_overrides\..*|start_type_description_service)$'
  critical:                 # "node:param" globs needing a double confirmation
    - '/controller_manager:update_rate'
    - '*:robot_ip'

topics:
  echo_max_rate_hz: 15.0
  echo_truncate_length: 64
  heavy_types: [sensor_msgs/msg/Image, sensor_msgs/msg/PointCloud2]

graph:
  hidden_node_regex: '^/(_ros2cli|launch_ros|transform_listener_impl|robot_dashboard_)'
  max_elements: 600

robot:
  joint_states_topic: /joint_states
  robot_description_topic: /robot_description
  near_limit_margin: 0.05   # fraction of the joint range

launch:
  sigint_timeout_s: 10.0
  sigterm_timeout_s: 5.0

safety:
  risky_service_words: [stop, reset, enable, disable, switch, start, power, brake]

health:
  default_profile: ros2_control_cell

core:
  executor: single          # single (recommended) | multi

qt:
  private_font_cache: auto  # auto | on | off (see README, "Notes")
```

See `config/dashboard.yaml` and `core/config.py` for every key.

## Launch profiles

```yaml
profiles:
  - id: my_cell                     # unique id; user files override examples with the same id
    name: My cell
    description: Driver, controllers and MoveIt
    command: ros2 launch my_cell_bringup bringup.launch.py
    cwd: ~                          # optional
    env: { RCUTILS_COLORIZED_OUTPUT: '0' }   # optional
    source: ~/other_ws/install/setup.bash    # optional, sourced before the command
    args:
      - name: sim
        type: bool                  # bool | int | float | string | choice
        default: true
        description: Use simulation
      - name: robot_ip
        type: string
        default: 192.168.1.10
        enabled_if: not sim         # widget enabled only when the expression is true
      - name: robot
        type: choice
        choices: [left, right]
        default: left
    presets:
      - name: Real robot
        values: { sim: false, robot_ip: 192.168.1.10 }
    stop:
      sigint_timeout_s: 10
      sigterm_timeout_s: 5

scenarios:
  - id: full_cell
    name: Full cell
    steps:
      - profile: my_cell
        preset: Real robot
        wait_node: /controller_manager      # wait until this node exists
        timeout_s: 30
      - profile: moveit
        wait_service: /move_action/_action/send_goal
      - profile: my_app
        wait_s: 2
```

Arguments are passed as `name:=value`. Disabled arguments are not passed, and neither
are empty values, except for string arguments. The Launch page's **New…** dialog writes this file for you. It
can also import the arguments of a launch file.

## Health profiles

```yaml
profiles:
  - id: ros2_control_cell
    name: ros2_control cell
    topics:
      - topic: /joint_states
        expected_hz: 100        # 0: only check that messages arrive
        tolerance: 0.2          # warn below expected_hz * (1 - tolerance)
        timeout_s: 1.0          # error after this time without messages
        max_latency_ms: 20      # optional, needs header.stamp
```

A topic is an error when no message has arrived within `timeout_s`, or when its rate is
below half the lower bound. It is a warning when the rate is below the lower bound or the
latency is too high.

## Parameter presets

A preset is a YAML file in the `ros2 param dump` format:

```yaml
/scaled_joint_trajectory_controller:
  ros__parameters:
    constraints:
      goal_time: 0.5
```

Loading a preset never applies it directly. It marks the differences as *modified*, so
you can review them and then click **Apply all**.
