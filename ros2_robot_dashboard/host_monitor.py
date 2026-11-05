"""host_monitor: publishes CPU, memory, network and temperature of this PC.

Run it on every PC of the ROS network you want to see in the dashboard Health page:

    ros2 run ros2_robot_dashboard host_monitor

It publishes ``diagnostic_msgs/DiagnosticArray`` on ``/<host>/host_metrics``
(and optionally on ``/diagnostics``, so the data also appears in the Diagnostics page).
"""

from __future__ import annotations

import re
import socket
import sys


def sanitize_host(name: str) -> str:
    """A host name usable as a ROS name component."""
    s = re.sub(r'[^A-Za-z0-9_]', '_', name)
    if not s or s[0].isdigit():
        s = 'host_' + s
    return s


def main(argv=None) -> int:
    import rclpy
    from diagnostic_msgs.msg import DiagnosticArray
    from rclpy.node import Node

    from .core.services.host_metrics import LocalMetrics, metrics_to_diagnostics

    rclpy.init(args=argv if argv is not None else sys.argv)
    host = socket.gethostname()
    safe = sanitize_host(host)
    node = Node(f'host_monitor_{safe}')
    period = node.declare_parameter('period_s', 1.0).value
    also_diag = node.declare_parameter('publish_diagnostics', False).value
    topic = node.declare_parameter('topic', f'/{safe}/host_metrics').value
    pub = node.create_publisher(DiagnosticArray, topic, 10)
    diag_pub = node.create_publisher(DiagnosticArray, '/diagnostics', 10) if also_diag else None
    metrics = LocalMetrics(host)

    def tick() -> None:
        msg = metrics_to_diagnostics(metrics.sample(), node.get_clock().now().to_msg())
        pub.publish(msg)
        if diag_pub is not None:
            diag_pub.publish(msg)

    node.create_timer(float(period), tick)
    node.get_logger().info(f'publishing host metrics of {host} on {topic}')
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, Exception):  # noqa: BLE001 - ExternalShutdownException
        pass
    finally:
        node.destroy_node()
        try:
            rclpy.shutdown()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
