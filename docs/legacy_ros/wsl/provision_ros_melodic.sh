#!/usr/bin/env bash
# Provision a fresh WSL2 Ubuntu 18.04 distro into the "ros-melodic" dev
# environment this repo expects.
#
# Run this ONCE, inside the fresh distro, after `wsl --import ros-melodic ...`
# (see setup/wsl/README.md). It is idempotent-ish: re-running re-syncs and
# rebuilds.
#
#   bash /mnt/c/Users/<you>/Documents/GeGI_Driver/ros_phds_gegi_driver/setup/wsl/provision_ros_melodic.sh
#
# Override the Windows repo location if yours differs:
#   WIN_REPO=/mnt/c/path/to/ros_phds_gegi_driver bash provision_ros_melodic.sh
set -euo pipefail

# --- Where the Windows repo (single source of truth) is mounted -------------
WIN_REPO="${WIN_REPO:-/mnt/c/Users/Aliyu.Bala/Documents/GeGI_Driver/ros_phds_gegi_driver}"
WS="${WS:-$HOME/gegi_ws}"

if [ ! -d "$WIN_REPO" ]; then
    echo "ERROR: Windows repo not found at: $WIN_REPO" >&2
    echo "       Pass the correct path with WIN_REPO=/mnt/c/... " >&2
    exit 1
fi

echo "==> Repo:      $WIN_REPO"
echo "==> Workspace: $WS"

# --- 1. ROS Melodic apt source + key ----------------------------------------
if [ ! -f /opt/ros/melodic/setup.bash ]; then
    echo "==> Installing ROS Melodic ..."
    sudo apt-get update
    sudo apt-get install -y curl gnupg lsb-release
    sudo sh -c 'echo "deb http://packages.ros.org/ros/ubuntu $(lsb_release -sc) main" > /etc/apt/sources.list.d/ros-latest.list'
    curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.asc | sudo apt-key add -
    sudo apt-get update
    sudo apt-get install -y ros-melodic-desktop
    sudo apt-get install -y python-rosdep python-rosinstall python-rosinstall-generator python-wstool build-essential
    sudo rosdep init || true
    rosdep update || true
else
    echo "==> ROS Melodic already present, skipping base install."
fi

# --- 2. App-specific apt deps (mirrors Dockerfile + setup_dependencies.sh) ---
echo "==> Installing app dependencies ..."
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    libboost-system-dev \
    libboost-regex-dev \
    libboost-thread-dev \
    ros-melodic-tf2-geometry-msgs \
    python3-pip \
    python3-matplotlib \
    python3-numpy \
    python-scipy \
    ros-melodic-rosbag \
    ros-melodic-rosbridge-server \
    ros-melodic-rosbridge-suite

# --- 3. Create the catkin workspace -----------------------------------------
mkdir -p "$WS/src"

# --- 4. Generate the helper scripts (these live OUTSIDE the repo) ------------
echo "==> Writing helper scripts to $WS ..."

cat > "$WS/sync.sh" <<EOF
#!/usr/bin/env bash
# Re-copy source from the Windows repo, strip CRLF, install Python nodes into devel.
set -euo pipefail
WIN_REPO="\${WIN_REPO:-$WIN_REPO}"
WS="\${WS:-$WS}"

DST="\$WS/src/phds_gegi_driver"
mkdir -p "\$DST"
# Copy the package source + the vendored msgs dep into the catkin src space.
rsync -a --delete --exclude '.git' --exclude 'data/' --exclude 'devel/' --exclude 'build/' \\
    "\$WIN_REPO/" "\$DST/"
rsync -a --delete "\$WIN_REPO/deps/radiation_detector_msgs/" \\
    "\$WS/src/radiation_detector_msgs/"

# Strip CRLF on shell + python so bash/python2 on Linux are happy.
find "\$DST" "\$WS/src/radiation_detector_msgs" -type f \\( -name '*.sh' -o -name '*.py' \\) -print0 \\
    | xargs -0 sed -i 's/\r\$//'

# catkin_install_python is install-only; mirror the Dockerfile by copying the
# Python nodes into devel/lib so roslaunch finds them after a Python-only edit.
if [ -d "\$WS/devel/lib/phds_gegi_driver" ]; then
    for n in spectrum_node spherical_heatmap_node activity_node data_recorder_node; do
        cp "\$DST/src/phds_gegi_driver/\$n.py" "\$WS/devel/lib/phds_gegi_driver/\$n.py"
    done
    sed -i 's/\r\$//' "\$WS/devel/lib/phds_gegi_driver/"*.py
    chmod +x "\$WS/devel/lib/phds_gegi_driver/"*.py
fi
echo "sync complete."
EOF

cat > "$WS/build.sh" <<EOF
#!/usr/bin/env bash
# sync.sh + catkin_make. Run after C++ changes.
set -euo pipefail
WS="\${WS:-$WS}"
"\$WS/sync.sh"
# ROS setup.bash references unset vars; relax nounset while sourcing.
set +u; source /opt/ros/melodic/setup.bash; set -u
cd "\$WS"
catkin_make
EOF

cat > "$WS/run.sh" <<EOF
#!/usr/bin/env bash
# Launch the full pipeline.
set -euo pipefail
WS="\${WS:-$WS}"
set +u; source /opt/ros/melodic/setup.bash; source "\$WS/devel/setup.bash"; set -u
roslaunch phds_gegi_driver gegi_full_pipeline.launch "\$@"
EOF

chmod +x "$WS/sync.sh" "$WS/build.sh" "$WS/run.sh"

# --- 5. First sync + build --------------------------------------------------
echo "==> Initial sync + build ..."
"$WS/sync.sh"
# ROS setup.bash references unset vars; relax nounset while sourcing.
set +u; source /opt/ros/melodic/setup.bash; set -u
cd "$WS"
catkin_make
# Re-run sync now that devel/lib exists so the Python nodes land there too.
"$WS/sync.sh"

# --- 6. Make the login shell source ROS + the workspace ---------------------
BRC="$HOME/.bashrc"
if ! grep -q 'gegi_ws/devel/setup.bash' "$BRC" 2>/dev/null; then
    {
        echo ''
        echo '# --- GeGI driver environment ---'
        echo 'source /opt/ros/melodic/setup.bash'
        echo "source $WS/devel/setup.bash"
    } >> "$BRC"
    echo "==> Added ROS + workspace sourcing to $BRC"
fi

echo ''
echo "==> DONE. Open a fresh shell, then:"
echo "    cd $WS && ./run.sh"
echo "    bash $WS/src/phds_gegi_driver/test/run_tests.sh   # verify (no hardware)"
