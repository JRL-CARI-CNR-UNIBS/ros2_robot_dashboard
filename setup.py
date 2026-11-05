import os
from glob import glob

from setuptools import find_packages, setup

package_name = 'ros2_robot_dashboard'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test', 'test.*']),
    package_data={package_name: ['adapters/qt/resources/*']},
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        (os.path.join('share', package_name, 'config'), glob('config/*.yaml')),
        (os.path.join('share', package_name, 'config', 'launch_profiles'),
         glob('config/launch_profiles/*.yaml')),
        (os.path.join('share', package_name, 'config', 'health_profiles'),
         glob('config/health_profiles/*.yaml')),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='Manuel Beschi',
    maintainer_email='manuel.beschi@unibs.it',
    description='Robot-agnostic ROS 2 dashboard (PySide6)',
    license='Apache-2.0',
    entry_points={
        'console_scripts': [
            'robot_dashboard = ros2_robot_dashboard.main:main',
            'host_monitor = ros2_robot_dashboard.host_monitor:main',
        ],
    },
)
