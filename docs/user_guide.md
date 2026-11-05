# User guide

This guide walks through every page of the dashboard using a Universal Robots arm
with **mock hardware**, so you need no real robot. The screenshots come from ROS 2
Jazzy. Humble and Lyrical look the same, though some controller names differ.

## Before you start

Install the UR driver and `ros2_control` in your workspace. With pixi:

```bash
pixi add ros-jazzy-ur ros-jazzy-ros2-control ros-jazzy-ros2-controllers
```

Then start the dashboard:

```bash
ros2 run ros2_robot_dashboard robot_dashboard
```

The left column lists the pages. The status bar shows the core state, `ROS_DOMAIN_ID`,
the detected distro, the node count, a button for recent errors, and a **READ-ONLY**
badge when write actions are disabled (*Safety → Read-only mode*, or `--read-only`).

Press **Ctrl+K** anywhere to search nodes, topics, services, actions, parameters and
frames, then jump to the right page.

## Tutorial 1: start the cell from the Launch page

![Launch page](images/launch.png)

1. Open **Launch** and select the example profile **UR cell (mock hardware)**.
2. Check the arguments. Each one has a typed widget (choice, bool, string, number).
   On Humble the mock option is called `use_fake_hardware`; on newer distros it is
   `use_mock_hardware`.
3. The command line below the form shows exactly what will run. Click **Copy** to use
   it in a terminal.
4. Click **Start**. The output appears live, and the profile shows `[running]`.

**Stop** sends SIGINT, then SIGTERM and SIGKILL if the process does not exit within the
profile's timeouts. Afterwards the page checks for leftover child processes, and
**Kill leftovers** removes them. **Save preset…** stores the current argument values.
**New…** creates a profile and can import the arguments of a launch file
(`ros2 launch --show-args`). Scenarios start several profiles in order and wait for a
node or service between steps.

When you close the window while processes are running, the dashboard asks whether to
leave them running or stop them.

## Tutorial 2: tune controller parameters

![Parameters page](images/params.png)

1. Open **Parameters** and pick `/scaled_joint_trajectory_controller`.
2. Parameters are grouped by their dotted prefix (`constraints.elbow_joint.goal` and so
   on). Read-only parameters are grey.
3. Double-click a value to edit it. Numbers with a declared range get a slider, and
   arrays open an editor.
4. With **Auto-apply** on, the value is sent after a short pause. With it off, edited
   rows are marked *modified* until you click **Apply all**. Check **Atomic** to apply
   all of them together, or none.
5. If another tool changes a parameter you are editing, the row shows a conflict.
   Right-click it to choose **keep mine** or **accept external**.

**Save preset…** writes the parameters to YAML (in `$ROS_HOME/robot_dashboard/param_presets`
by default). **Load preset…** marks the differences as modified so you can review
them before applying. Parameters listed in `params.critical` need a second confirmation.

## Tutorial 3: inspect topics

![Topics page](images/topics.png)

- The table lists every topic with its type and publisher and subscriber counts.
  Select topics and click **Measure rate** to add rate and bandwidth.
- Selecting a topic shows each endpoint with its QoS. QoS mismatches (for example a
  best-effort publisher and a reliable subscriber) are reported below.
- **Echo** prints messages, sampled at most at *max* msg/s, so even 500 Hz
  `/joint_states` stays readable. Long arrays are truncated. **Header only** and the
  QoS choice help with large or special topics.
- **Record to file…** saves every message as YAML text, without sampling or truncation,
  optionally with receive time and `header.stamp`, and with a message or time limit.

## Tutorial 4: call services and send action goals

![Services page](images/services.png)

Select a service. The page shows its definition and a request form generated from it.
Fill the fields (or switch to **YAML**) and click **Call**. The response appears below.
Calls to services whose name looks risky (`stop`, `reset`, `switch`…) ask for
confirmation first.

![Actions page](images/actions.png)

Actions work the same way. In this example a `FollowJointTrajectory` goal is sent to
`/scaled_joint_trajectory_controller/follow_joint_trajectory`. Feedback messages
stream into the lower panel, followed by the result. **Cancel goal** cancels the goal.

## Tutorial 5: plot the motion

![Plot](images/utilities.png)

In **Utilities → Plot**, choose a topic and a numeric field (`position[0]`,
`velocity[0]`…) and click **Add**. The plot above was recorded while the goal from
Tutorial 4 moved two joints. **Export CSV…** saves the visible data. **Compare
parameters** shows the differences between two nodes, or between a node and a preset
file.

## Tutorial 6: robot state and TF

![Robot page](images/robot.png)

**Robot** shows each joint with a bar between its URDF limits. Joints close to a limit
are highlighted. **Degrees** switches the units. The right side summarises the URDF from
`/robot_description` and shows the pose of a *child* frame in a *parent* frame.

![TF page](images/tf.png)

**TF** shows the frame tree with publishing rates and the age of the last update.
Problems are listed below the tree: multiple parents or roots, loops, and stale frames.
To start an echo, pick the target and source frames and a format: quaternion, RPY in
radians or degrees, or a 4×4 SE(3) matrix. **Swap** inverts the transform.

## Tutorial 7: switch controllers

![Controllers page](images/controllers.png)

**Controllers** lists the controllers of the selected controller manager with their state,
type, update rate and claimed interfaces. Selecting an inactive controller previews what a
switch would do: which interfaces it claims, and which active controllers conflict with it.

**Switch…** opens a dialog to activate and deactivate several controllers in one step
(strict or best-effort, with a timeout). The tutorial run swaps
`scaled_joint_trajectory_controller` for `forward_position_controller` and back.
Right-click a controller to configure, unload or clean it up, or to open its parameters.

![Hardware interfaces](images/controllers_interfaces.png)

The **Hardware interfaces** and **Hardware components** tabs show every interface with
its claim status, and let you change a component's lifecycle state.

## Tutorial 8: graph

![Graph page](images/graph.png)

**Graph** draws nodes (ellipses) and topics (boxes). By default it hides debug topics,
hidden nodes, topics with only one side, and nodes left without connections. Untick
these options to see everything. Use **Only neighbours of selection** to focus on one
node. You can drag nodes, and their positions are remembered. **Export…** writes PNG,
SVG or DOT.

## Tutorial 9: logs, diagnostics, lifecycle

![Logs](images/logs.png)

The **Logs** tab collects `/rosout`. Filter it by minimum level, by node (multi-select)
and by regex. You can pause, follow and export it. The **Diagnostics** tab groups
`/diagnostics` statuses by name prefix (`prefix: item`) and keeps a short history for each status. **Lifecycle**
lists managed nodes and triggers their transitions.

## Tutorial 10: health

![Health page](images/health.png)

A *health profile* lists critical topics with their expected rate, tolerance and
timeout. The example profile `ros2_control cell` checks `/joint_states`, `/tf` and
`/diagnostics`. A row turns yellow or red when the rate is too low or messages stop,
and the event is logged under **Alerts**.

The host table shows CPU, memory, load, network and temperature of this PC. To add robot
PCs, run this on each one:

```bash
ros2 run ros2_robot_dashboard host_monitor
```

## Tutorial 11: bags

![Bags page](images/bags.png)

**Record** lets you choose topics (or all), the storage format, compression and split
size. The command preview shows the `ros2 bag record` command that will run. The page
warns before recording heavy topics such as images and point clouds. **Play** shows a
bag's info and plays it with rate, loop and `/clock` options. You can pause and resume
playback.

## Dark theme

![Dark theme](images/dark_theme.png)

Use *View → Dark theme* or `--theme dark`.

## Safety

- **Read-only mode** disables every widget that writes to the robot.
- **Confirmations**: risky services, controller switches, critical parameters, and
  stopping processes ask first (`confirm_actions: false` disables them).
- **Action log**: every write action is appended to
  `~/.local/state/ros2_robot_dashboard/actions.log` (*Safety → Action log*).
