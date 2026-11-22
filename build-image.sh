#! /bin/bash
# shellcheck disable=SC2129

set -e
set -x
set -o pipefail

if [ "$EUID" -ne 0 ]; then
	echo "$(basename "$0") must be run as root"
	exit 1
fi

TEST_BUILD=""
NO_COMPRESS=""
LOCAL_BUILD=""

BUILD_USER=${BUILD_USER:-}
OUTPUT_DIR=${OUTPUT_DIR:-}

BUILD_BRANCH=${BUILD_BRANCH:-}

source manifest
source sub-manifest

if [ -z "${SYSTEM_NAME}" ]; then
	echo "SYSTEM_NAME must be specified"
	exit
fi

if [ -z "${VERSION}" ]; then
	echo "VERSION must be specified"
	exit
fi

if [ -z "${BUILD_BRANCH}" ]; then
  echo "BUILD_BRANCH must be specified"
  exit 1
fi

DISPLAY_VERSION=${VERSION}
LSB_VERSION=${VERSION}
VERSION_NUMBER=${VERSION}

UNIFIED_VERSION=${VERSION}
UNIFIED_DISPLAY_VERSION=${DISPLAY_VERSION}

if [ -n "$1" ]; then
	DISPLAY_VERSION="${VERSION} (${1}-${BUILD_BRANCH})"
	UNIFIED_DISPLAY_VERSION="${UNIFIED_DISPLAY_VERSION} (${1})"
	VERSION="${VERSION}_${1}-${BUILD_BRANCH}"
	UNIFIED_VERSION="${UNIFIED_VERSION}_${1}"
	LSB_VERSION="${LSB_VERSION}　(${1}-${BUILD_BRANCH})"
	BUILD_ID="${1}-${BUILD_BRANCH}"
fi

BUILD_DATE=$(date +%Y%m%d)

export BUILD_ID=${BUILD_DATE}_${BUILD_ID}
export FULL_VERSION=${VERSION}
export DISPLAY_VERSION=${DISPLAY_VERSION}
export LSB_VERSION=${LSB_VERSION}
export VERSION_NUMBER=${VERSION_NUMBER}
export UNIFIED_VERSION=${UNIFIED_VERSION}
export UNIFIED_DISPLAY_VERSION=${UNIFIED_DISPLAY_VERSION}

MOUNT_PATH=/tmp/${SYSTEM_NAME}-build
BUILD_PATH=${MOUNT_PATH}/subvolume
SNAP_PATH=${MOUNT_PATH}/${SYSTEM_NAME}-${VERSION}
BUILD_IMG=/output/${SYSTEM_NAME}-build.img

mkdir -p ${MOUNT_PATH}

fallocate -l ${SIZE} ${BUILD_IMG}
mkfs.btrfs -f ${BUILD_IMG}
mount -t btrfs -o loop,nodatacow ${BUILD_IMG} ${MOUNT_PATH}
btrfs subvolume create ${BUILD_PATH}

before_chroot() {
	umount ${BUILD_PATH} 2>/dev/null || true

	# copy the makepkg.conf into chroot
	cp /etc/makepkg.conf rootfs/etc/makepkg.conf

	# bootstrap using our configuration
	pacstrap -K -C rootfs/etc/pacman.conf ${BUILD_PATH}

	# copy the builder mirror list into chroot
	mkdir -p rootfs/etc/pacman.d
	cp /etc/pacman.d/mirrorlist rootfs/etc/pacman.d/mirrorlist

	# copy files into chroot
	cp -R manifest sub-manifest base-* postinstall all-install.sh rootfs/. ${BUILD_PATH}/

	mkdir -p ${BUILD_PATH}/pre-download
	rm -rf ${BUILD_PATH}/pre-download/*
	cp -rv pre-download/*.tar.gz ${BUILD_PATH}/pre-download

	mkdir -p ${BUILD_PATH}/override_pkgs
	rm -rf ${BUILD_PATH}/override_pkgs/*
	mv ${BUILD_PATH}/extra/*.pkg.tar.zst ${BUILD_PATH}/override_pkgs/ || true

	if [ -n "${PACKAGE_OVERRIDES_URLS}" ]; then
		wget --directory-prefix=${BUILD_PATH}/override_pkgs ${PACKAGE_OVERRIDES_URLS}
	fi

	# chroot into target
	mount --bind ${BUILD_PATH} ${BUILD_PATH}
	
	return 0
}

if [ -z "${TEST_BUILD}" ]; then

	# 重试次数
	MAX_RETRIES=3
	RETRY_COUNT=0
	INSTALL_OK=0

	while [ ${RETRY_COUNT} -lt ${MAX_RETRIES} ]; do
		RETRY_COUNT=$((RETRY_COUNT + 1))
		echo ">>>>>> All install  (${RETRY_COUNT}/${MAX_RETRIES})"
		if ! before_chroot; then
			continue
		fi
		if ! arch-chroot ${BUILD_PATH} /bin/bash -c "cd / && /all-install.sh"; then
			continue
		fi
		INSTALL_OK=1
		break
	done

	if [ ${INSTALL_OK} -ne 1 ]; then
		echo ">>>>>> All install failed after ${MAX_RETRIES} attempts. Stopping..."
		exit 1
	fi
else
	echo "Test build, skipping all install"
fi

rm ${BUILD_PATH}/all-install.sh
rm ${BUILD_PATH}/postinstall
rm ${BUILD_PATH}/sub-manifest
rm ${BUILD_PATH}/manifest
rm ${BUILD_PATH}/base-*

#defrag the image
btrfs filesystem defragment -r ${BUILD_PATH}

# copy files into chroot again
cp -R rootfs/. ${BUILD_PATH}/
rm -rf ${BUILD_PATH}/extra

echo "${SYSTEM_NAME}-${VERSION}" >${BUILD_PATH}/build_info
echo "" >>${BUILD_PATH}/build_info
cat ${BUILD_PATH}/package-list >>${BUILD_PATH}/build_info
rm ${BUILD_PATH}/package-list

# freeze archive date of build to avoid package drift on unlock
# if no archive date is set
if [ -z "${ARCHIVE_DATE}" ]; then
	TODAY_DATE=$(date +%Y/%m/%d)
	export TODAY_DATE
	echo "Server=https://archive.archlinux.org/repos/${TODAY_DATE}/\$repo/os/\$arch" > \
		${BUILD_PATH}/etc/pacman.d/mirrorlist
fi

# arch-chroot temporarily bind-mounts its resolver during installation.
# Set the final image link after chroot teardown and the last rootfs copy.
if [ -z "${TEST_BUILD}" ]; then
	ln -sfnT /usr/lib/systemd/resolv.conf "${BUILD_PATH}/etc/resolv.conf"
fi

# show free space before snapshot
echo "Free space"
df -h

COMRESS_ON_THE_FLY="1"

btrfs subvolume snapshot -r ${BUILD_PATH} ${SNAP_PATH}

# 
IMG_EXT=".img"

# Compression method: xz or zstd
COMPRESS_METHOD="${COMPRESS_METHOD:-xz}"

# Set compression level based on method
if [ "${COMPRESS_METHOD}" = "zstd" ]; then
    COMPRESS_LEVEL="${COMPRESS_LEVEL:--15}"
    COMPRESS_EXT=".zst"
else
    COMPRESS_LEVEL="${COMPRESS_LEVEL:--7}"
    COMPRESS_EXT=".xz"
fi

SPECIAL_IMG_EXT=".skosys"

IMG_FILENAME_WITHOUT_EXT="${SYSTEM_NAME}-${VERSION}"
if [ -z "${NO_COMPRESS}" ]; then
	if [ -n "${COMRESS_ON_THE_FLY}" ]; then
		# IMG_FILENAME="${IMG_FILENAME_WITHOUT_EXT}${IMG_EXT}${COMPRESS_EXT}"
		IMG_FILENAME="${IMG_FILENAME_WITHOUT_EXT}${SPECIAL_IMG_EXT}"
		btrfs send ${SNAP_PATH} | ${COMPRESS_METHOD} ${COMPRESS_LEVEL} -T0 >${IMG_FILENAME}
	else
		IMG_FILENAME="${IMG_FILENAME_WITHOUT_EXT}${IMG_EXT}.tar${COMPRESS_EXT}"
		btrfs send -f ${IMG_FILENAME_WITHOUT_EXT}${IMG_EXT} ${SNAP_PATH}
		tar -c -I"${COMPRESS_METHOD} ${COMPRESS_LEVEL} -T0" -f ${IMG_FILENAME} ${IMG_FILENAME_WITHOUT_EXT}${IMG_EXT}
		rm ${IMG_FILENAME_WITHOUT_EXT}${IMG_EXT}
	fi
else
	IMG_FILENAME="${IMG_FILENAME_WITHOUT_EXT}${IMG_EXT}"
	btrfs send -f ${IMG_FILENAME} ${SNAP_PATH}
fi


cp ${BUILD_PATH}/build_info build_info-${BUILD_BRANCH}.txt

# clean up
umount -l ${BUILD_PATH}
umount -l ${MOUNT_PATH}
rm -rf ${MOUNT_PATH}
rm -rf ${BUILD_IMG}

if [ -z "${LOCAL_BUILD}" ]; then
	sha256sum ${IMG_FILENAME_WITHOUT_EXT}* >sha256sum-${BUILD_BRANCH}.txt
	cat sha256sum-${BUILD_BRANCH}.txt

	# Move the image to the output directory, if one was specified.
	if [ -n "${OUTPUT_DIR}" ]; then
		mkdir -p "${OUTPUT_DIR}"
		mv ${IMG_FILENAME_WITHOUT_EXT}* ${OUTPUT_DIR} || true
		mv build_info-${BUILD_BRANCH}.txt ${OUTPUT_DIR}
		mv sha256sum*.txt ${OUTPUT_DIR} || true
	fi

	# set outputs for github actions
	if [ -f "${GITHUB_OUTPUT}" ]; then
		echo "version=${UNIFIED_VERSION}" >>"${GITHUB_OUTPUT}"
		echo "display_version=${UNIFIED_DISPLAY_VERSION}" >>"${GITHUB_OUTPUT}"
		echo "display_name=${SYSTEM_DESC}" >>"${GITHUB_OUTPUT}"
		echo "image_filename=${IMG_FILENAME}" >>"${GITHUB_OUTPUT}"
		echo "image_filename_without_ext=${IMG_FILENAME_WITHOUT_EXT}" >>"${GITHUB_OUTPUT}"
	else
		echo "No github output file set"
	fi
else
	echo "Local build, output IMG directly"
	if [ -n "${OUTPUT_DIR}" ]; then
		mkdir -p "${OUTPUT_DIR}"
		mv ${IMG_FILENAME_WITHOUT_EXT}${IMG_EXT} ${OUTPUT_DIR}
	fi
fi
