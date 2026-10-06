#!/bin/bash
# build-delta.sh - 在两个 SkorionOS 版本之间生成增量更新包
# 供自动和手动增量工作流共用
#
# 输入:  两个 .skosys 文件 (btrfs send 流经 xz 压缩)
# 输出:  .skdelta 文件经 xz 压缩、manifest 片段、sha256sum
#
# 支持两种增量格式 (--delta-format):
#   rsync-batch  - rsync --write-batch 二进制差异（更小，需 rsync 3.4+ 配合 --no-inc-recursive）
#   tar          - tar 打包变更文件 + 控制清单（兼容性更好）

set -euo pipefail

usage() {
    cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Required:
  --target-img PATH    Target (new) version .skosys file path
  --base-img PATH      Base (old) version .skosys file path
  --output-dir DIR     Output directory for delta files

Optional:
  --target-name NAME   Target subvolume name (auto-derived from filename)
  --base-name NAME     Base subvolume name (auto-derived from filename)
  --base-tag TAG       Base version release tag (auto-derived from version)
  --max-ratio PCT      Max delta/full size ratio percentage (default: 70)
  --work-size SIZE     Temp btrfs image size (default: auto-calculated)
  --delta-format FMT   Delta format: rsync-batch (default) or tar
EOF
    exit 1
}

# 从子卷名中提取版本号
# 例: skorionos-50-4_5d150d2-gnome-nv -> 50-4_5d150d2
#     chimeraos-46_abc1234-gnome-core  -> 46_abc1234
# 正则: 匹配 "前缀-主版本号(-次版本号)?_commit哈希-后缀"，提取中间版本部分
extract_version() {
    echo "$1" | sed -n 's/\(chimeraos\|skorionos\)-\([0-9]\+\(-[0-9]\+\)\?_[a-f0-9]\+\)-.*/\2/p'
}

TARGET_IMG=""
BASE_IMG=""
TARGET_NAME=""
BASE_NAME=""
BASE_TAG=""
MAX_RATIO=70
OUTPUT_DIR=""
WORK_SIZE=""
WORK_SIZE_SET=false
DELTA_FORMAT="rsync-batch"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --target-img)  TARGET_IMG="$2";  shift 2 ;;
        --base-img)    BASE_IMG="$2";    shift 2 ;;
        --target-name) TARGET_NAME="$2"; shift 2 ;;
        --base-name)   BASE_NAME="$2";   shift 2 ;;
        --base-tag)    BASE_TAG="$2";    shift 2 ;;
        --max-ratio)   MAX_RATIO="$2";   shift 2 ;;
        --output-dir)  OUTPUT_DIR="$2";  shift 2 ;;
        --work-size)   WORK_SIZE="$2"; WORK_SIZE_SET=true; shift 2 ;;
        --delta-format) DELTA_FORMAT="$2"; shift 2 ;;
        -h|--help)     usage ;;
        *) echo "Unknown option: $1" >&2; usage ;;
    esac
done

if [ -z "$TARGET_IMG" ] || [ -z "$BASE_IMG" ] || [ -z "$OUTPUT_DIR" ]; then
    echo "Error: --target-img, --base-img, and --output-dir are required" >&2
    usage
fi

if [ ! -f "$TARGET_IMG" ]; then
    echo "Error: target image not found: $TARGET_IMG" >&2
    exit 1
fi
if [ ! -f "$BASE_IMG" ]; then
    echo "Error: base image not found: $BASE_IMG" >&2
    exit 1
fi

[ -z "$TARGET_NAME" ] && TARGET_NAME=$(basename "$TARGET_IMG" .skosys)
[ -z "$BASE_NAME" ]   && BASE_NAME=$(basename "$BASE_IMG" .skosys)

TARGET_VERSION=$(extract_version "$TARGET_NAME")
BASE_VERSION=$(extract_version "$BASE_NAME")

if [ -z "$TARGET_VERSION" ] || [ -z "$BASE_VERSION" ]; then
    echo "Error: cannot extract version from names" >&2
    echo "  target: $TARGET_NAME -> $TARGET_VERSION" >&2
    echo "  base:   $BASE_NAME -> $BASE_VERSION" >&2
    exit 1
fi

[ -z "$BASE_TAG" ] && BASE_TAG="$BASE_VERSION"

if [ "$DELTA_FORMAT" != "rsync-batch" ] && [ "$DELTA_FORMAT" != "tar" ]; then
    echo "Error: --delta-format must be 'rsync-batch' or 'tar', got '$DELTA_FORMAT'" >&2
    exit 1
fi

DELTA_FILENAME="${TARGET_NAME}.from_${BASE_VERSION}.skdelta"

echo "=== Delta Generation ==="
echo "  Target: $TARGET_NAME ($TARGET_VERSION)"
echo "  Base:   $BASE_NAME ($BASE_VERSION)"
echo "  Format: $DELTA_FORMAT"
echo "  Output: $DELTA_FILENAME"

mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR=$(realpath "$OUTPUT_DIR")
DELTA_FILE="$OUTPUT_DIR/$DELTA_FILENAME"
FULL_SIZE=$(stat -c %s "$TARGET_IMG")

# 只删除本次生成的临时文件，绝不删除调用者传入的 .skosys。
# 提前注册 trap，覆盖 mktemp/fallocate/mkfs/receive 失败。
WORK_DIR=""
WORK_IMG=""
DELTA_STAGING=""

release_work() {
    if [ -n "$WORK_DIR" ] && [ -d "$WORK_DIR" ]; then
        if mountpoint -q "$WORK_DIR"; then
            if ! umount "$WORK_DIR"; then
                echo "Error: cannot unmount $WORK_DIR; preserving it and $WORK_IMG" >&2
                return 1
            fi
        fi
        # 不使用 rm -rf：即使挂载检查失败，也不能递归删除仍挂载的子卷。
        if ! rmdir "$WORK_DIR"; then
            echo "Error: cannot remove work mountpoint; preserving $WORK_IMG" >&2
            return 1
        fi
    fi
    WORK_DIR=""
    if [ -n "$WORK_IMG" ]; then
        rm -f -- "$WORK_IMG" || return 1
        WORK_IMG=""
    fi
}

cleanup() {
    local status=$?
    trap - EXIT
    if ! release_work; then
        status=1
    fi
    if [ -n "$DELTA_STAGING" ]; then
        rm -rf -- "$DELTA_STAGING" || status=1
    fi
    if [ "$status" -ne 0 ]; then
        rm -f -- "$DELTA_FILE" "$OUTPUT_DIR/delta-status.txt" \
            "$OUTPUT_DIR/delta-entry.json" "$OUTPUT_DIR/delta-sha256sum.txt"
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# 清掉上一次同目录运行留下的结果，失败时不能继续呈现旧的 OK。
rm -f -- "$DELTA_FILE" "$OUTPUT_DIR/delta-status.txt" \
    "$OUTPUT_DIR/delta-entry.json" "$OUTPUT_DIR/delta-sha256sum.txt"
WORK_DIR=$(mktemp -d /tmp/delta-work-XXXX)
WORK_IMG=$(mktemp /tmp/delta-img-XXXX.img)
DELTA_STAGING=$(mktemp -d "$OUTPUT_DIR/.delta-staging-XXXX")
DELTA_PARTIAL="$DELTA_STAGING/delta.skdelta.partial"

available_bytes() {
    df -B1 --output=avail "$1" | tail -1 | tr -d ' '
}

require_output_space() {
    local available
    available=$(available_bytes "$OUTPUT_DIR")
    if [ "$available" -lt "$OUTPUT_RESERVE" ]; then
        echo "Error: insufficient output disk space (available: $available bytes, estimated need: $OUTPUT_RESERVE bytes); use a smaller --work-size or a larger output filesystem" >&2
        return 1
    fi
}

# batch 最坏情况接近完整 target 数据，而不是压缩后的 .skosys 大小。
# 先用 xz 索引中的未压缩 send 流估算，留 10% 格式开销及原有 5GiB
# 系统/元数据余量。send 中的克隆/稀疏文件可能使此估计偏小，所以还原后
# 还会按 target 的实际 apparent size 复查；这些是保守预算，不是格式上界。
TARGET_STREAM_SIZE=$(xz --robot --list "$TARGET_IMG" | awk -F '\t' '$1 == "totals" {print $5}')
if ! [[ "$TARGET_STREAM_SIZE" =~ ^[0-9]+$ ]] || [ "$TARGET_STREAM_SIZE" -eq 0 ] || [ "$FULL_SIZE" -eq 0 ]; then
    echo "Error: cannot determine target image size" >&2
    exit 1
fi
SYSTEM_RESERVE=$((5 * 1024 * 1024 * 1024))
OUTPUT_RESERVE=$((TARGET_STREAM_SIZE + TARGET_STREAM_SIZE / 10 + SYSTEM_RESERVE))
require_output_space

if [ "$WORK_SIZE_SET" = false ]; then
    AVAIL_BYTES=$(available_bytes "$(dirname "$WORK_IMG")")
    WORK_RESERVE=$SYSTEM_RESERVE
    if [ "$(stat -c %d "$WORK_IMG")" = "$(stat -c %d "$OUTPUT_DIR")" ]; then
        WORK_RESERVE=$OUTPUT_RESERVE
    fi
    WORK_SIZE_GB=$(((AVAIL_BYTES - WORK_RESERVE) / 1024 / 1024 / 1024))
    if [ "$WORK_SIZE_GB" -lt 15 ]; then
        echo "Error: insufficient disk space for a 15GiB work filesystem plus $WORK_RESERVE reserved bytes" >&2
        exit 1
    fi
    WORK_SIZE="${WORK_SIZE_GB}G"
    echo "Auto-calculated work filesystem size: $WORK_SIZE (reserved: $WORK_RESERVE bytes)"
fi

echo "Creating temporary btrfs filesystem ($WORK_SIZE)..."
fallocate -l "$WORK_SIZE" "$WORK_IMG"
# 同样约束手动 --work-size，避免挤掉 batch/压缩输出的空间。
require_output_space
mkfs.btrfs -f "$WORK_IMG" > /dev/null
mount -t btrfs -o loop,nodatacow "$WORK_IMG" "$WORK_DIR"

# --- 还原两个版本的 btrfs 快照 ---
# .skosys 文件是 btrfs send 流经 xz 压缩的产物
# xz -dc 解压后通过管道传给 btrfs receive 还原为子卷
echo "Restoring target: $TARGET_NAME ..."
xz -dc "$TARGET_IMG" | btrfs receive --quiet "$WORK_DIR"

echo "Restoring base: $BASE_NAME ..."
xz -dc "$BASE_IMG" | btrfs receive --quiet "$WORK_DIR"

if [ ! -d "$WORK_DIR/$TARGET_NAME" ]; then
    echo "Error: target subvolume not found after btrfs receive" >&2
    echo "Available subvolumes:" >&2
    ls -1 "$WORK_DIR/" >&2
    exit 1
fi
if [ ! -d "$WORK_DIR/$BASE_NAME" ]; then
    echo "Error: base subvolume not found after btrfs receive" >&2
    echo "Available subvolumes:" >&2
    ls -1 "$WORK_DIR/" >&2
    exit 1
fi

# 按还原后的逻辑大小（包含稀疏区、独立 reflink 文件）复查输出预算。
# du 默认只计一次硬链接，与 rsync -H 一致；不能使用物理块占用估计 batch。
TARGET_APPARENT_SIZE=$(du -sb "$WORK_DIR/$TARGET_NAME" | cut -f1)
OUTPUT_RESERVE=$((TARGET_APPARENT_SIZE + TARGET_APPARENT_SIZE / 10 + SYSTEM_RESERVE))
require_output_space

# --- 生成目标 subvolume 元数据指纹（用于部署后校验） ---
# 遍历目标子卷的所有文件，收集每个文件的属性（路径/大小/权限/UID/GID/类型），
# 排序后取 sha256 得到整体指纹。部署增量包后对比此值可判断更新是否完整。
# - 排除 /proc /sys /dev /tmp /run：这些是运行时虚拟目录，不属于镜像内容
# - 排除 socket 文件（-not -type s）：rsync 会跳过它们，两边不一致会导致 hash 不匹配
# - LC_ALL=C sort：保证不同 locale 下排序结果一致
echo "Generating target metadata fingerprint..."
TARGET_META_HASH=$(cd "$WORK_DIR/$TARGET_NAME" && find . \
    -not -path './proc/*' -not -path './sys/*' -not -path './dev/*' \
    -not -path './tmp/*' -not -path './run/*' \
    -not -type s \
    -printf '%P\t%s\t%m\t%U\t%G\t%y\n' 2>/dev/null \
    | LC_ALL=C sort | sha256sum | awk '{print $1}')
echo "Target metadata hash: $TARGET_META_HASH"

# --- 生成 target 完整文件列表（用于部署后清理设备 baseline 中的多余文件） ---
# 设备上的 baseline 可能包含 CI baseline 中不存在的文件（如 fontconfig 缓存等运行时产生的文件）。
# delta 的删除清单只包含 CI baseline 中有但 target 中没有的文件，无法覆盖设备独有的多余文件。
# 将 target 完整文件列表打入 delta 包，部署后据此删除所有不在列表中的文件，确保精确匹配。
FILELIST_FILE="$DELTA_STAGING/.delta-filelist"
(cd "$WORK_DIR/$TARGET_NAME" && find . \
    -not -path './proc/*' -not -path './sys/*' -not -path './dev/*' \
    -not -path './tmp/*' -not -path './run/*' \
    -not -type s \
    -printf '%P\n' 2>/dev/null \
    | LC_ALL=C sort) > "$FILELIST_FILE"
FILELIST_COUNT=$(wc -l < "$FILELIST_FILE" | tr -d ' ')
echo "Target file list: $FILELIST_COUNT entries"

# --- 嵌入元数据供离线安装自检查 ---
cat > "$DELTA_STAGING/.delta-meta.json" <<EOF
{
  "from_version": "${BASE_VERSION}",
  "target_version": "${TARGET_VERSION}",
  "target_name": "${TARGET_NAME}",
  "format": "${DELTA_FORMAT}",
  "target_meta_hash": "${TARGET_META_HASH}"
}
EOF

# --- 生成增量包（按 DELTA_FORMAT 分支） ---

if [ "$DELTA_FORMAT" = "rsync-batch" ]; then
    # === rsync-batch 格式 ===
    # btrfs receive 创建的子卷带 received_uuid，需要 -f 强制解除只读
    echo "Setting base subvolume writable..."
    btrfs property set -fts "$WORK_DIR/$BASE_NAME" ro false

    # --no-inc-recursive: 避免 rsync 3.4.1 的 read-batch inc-recursive bug
    echo "Generating rsync batch (--no-inc-recursive --numeric-ids)..."
    rsync -aAXH --numeric-ids --no-inc-recursive --delete \
        --write-batch="$DELTA_STAGING/batch" \
        "$WORK_DIR/$TARGET_NAME/" "$WORK_DIR/$BASE_NAME/"

    BATCH_SIZE=$(stat -c %s "$DELTA_STAGING/batch")
    echo "  Batch file size: $(numfmt --to=iec "$BATCH_SIZE")"

    # batch 已包含部署所需数据，先卸载并释放大镜像，再写压缩输出。
    release_work
    # OUTPUT_DIR 可能在另一块盘上，释放工作镜像未必会增加这里的空间。
    OUTPUT_RESERVE=$((BATCH_SIZE + BATCH_SIZE / 10 + SYSTEM_RESERVE))
    require_output_space
    echo "Compressing delta with xz..."
    tar cf - -C "$DELTA_STAGING" batch .delta-filelist .delta-meta.json \
        | xz -7 -T0 > "$DELTA_PARTIAL"

else
    # === tar 格式 ===
    # rsync dry-run 找出变更/删除文件，tar 打包变更文件 + 控制清单
    echo "Comparing target and base subvolumes..."
    CHANGES_FILE="$DELTA_STAGING/changes.txt"
    DELETIONS_FILE="$DELTA_STAGING/.delta-deletions"
    MODIFIED_FILE="$DELTA_STAGING/modified.txt"
    ATTRS_FILE="$DELTA_STAGING/.delta-attrs"

    rsync -aAXH --delete --dry-run --itemize-changes \
        "$WORK_DIR/$TARGET_NAME/" "$WORK_DIR/$BASE_NAME/" \
        > "$CHANGES_FILE"

    true > "$DELETIONS_FILE"
    true > "$MODIFIED_FILE"
    true > "$ATTRS_FILE"

    # 解析 rsync itemize-changes 输出，精确分为三类：
    #   1. 内容变更（'>' '<' 'c' 'h' 开头）→ 打包完整文件到 tar
    #   2. 仅属性变更（'.' 开头且 p/o/g 位有变化）→ 记录到 .delta-attrs 清单
    #   3. 仅时间戳变化（'.' 开头只有 t 位变化）→ 忽略，不影响 metadata hash
    #
    # rsync itemize flags 各位含义 (YXcstpoguax):
    #   位0=更新类型 位1=文件类型 位2=checksum 位3=size 位4=timestamp
    #   位5=permissions 位6=owner 位7=group 位8=unused 位9=ACL 位10=xattr
    #
    # 每行格式示例:
    #   >f.st...... usr/bin/foo        — 内容变更的普通文件
    #   cL+++++++++ usr/lib/bar -> ..  — 变更的符号链接（附带 " -> 目标"后缀）
    #   hf......... usr/bin/foo => ..  — 变更的硬链接（附带 " => 目标"后缀）
    #   .f...p.g... usr/bin/baz        — 仅权限/组变更 → 属性清单
    #   .d..t...... usr/lib/dir/       — 仅时间戳变更 → 忽略
    #   *deleting   usr/old/file       — 需要删除的文件
    while IFS= read -r line; do
        change_type="${line:0:1}"
        # 提取路径: 去掉开头的 flags，再去掉符号链接 " -> " 和硬链接 " => " 后缀
        file_path=$(echo "$line" | sed -e 's/^[^ ]* //' -e 's/ -> .*//' -e 's/ => .*//')
        [ -z "$file_path" ] && continue
        [ "$file_path" = "./" ] && continue

        if [ "$change_type" = "*" ]; then
            del_path=$(echo "$line" | sed 's/^\*deleting   //')
            [ -n "$del_path" ] && echo "$del_path" >> "$DELETIONS_FILE"
        elif [ "$change_type" = "." ]; then
            p_flag="${line:5:1}"
            o_flag="${line:6:1}"
            g_flag="${line:7:1}"
            if [ "$p_flag" != "." ] || [ "$o_flag" != "." ] || [ "$g_flag" != "." ]; then
                file_path_clean="${file_path%/}"
                target_path="$WORK_DIR/$TARGET_NAME/$file_path_clean"
                mode=$(stat -c '%a' "$target_path")
                uid=$(stat -c '%u' "$target_path")
                gid=$(stat -c '%g' "$target_path")
                printf '%s\t%s\t%s\t%s\n' "$file_path_clean" "$mode" "$uid" "$gid" >> "$ATTRS_FILE"
            fi
        else
            echo "$file_path" >> "$MODIFIED_FILE"
        fi
    done < "$CHANGES_FILE"

    MOD_COUNT=$(wc -l < "$MODIFIED_FILE" | tr -d ' ')
    DEL_COUNT=$(wc -l < "$DELETIONS_FILE" | tr -d ' ')
    ATTR_COUNT=$(wc -l < "$ATTRS_FILE" | tr -d ' ')
    echo "  Modified/new files: $MOD_COUNT"
    echo "  Deleted files: $DEL_COUNT"
    echo "  Attribute-only changes: $ATTR_COUNT"

    if [ "$MOD_COUNT" -eq 0 ] && [ "$DEL_COUNT" -eq 0 ] && [ "$ATTR_COUNT" -eq 0 ]; then
        echo "No differences found between versions, skipping"
        release_work
        echo "SKIP" > "$OUTPUT_DIR/delta-status.txt"
        exit 0
    fi

    # 一个完整 tar 流，控制文件和 target 文件写入同一归档，不拼接 tar。
    # 此分支仍要读取 target，因此压缩管道完成后才能卸载工作文件系统。
    echo "Compressing delta with xz..."
    tar cf - --xattrs --acls --numeric-owner \
        -C "$DELTA_STAGING" .delta-deletions .delta-attrs .delta-filelist .delta-meta.json \
        -C "$WORK_DIR/$TARGET_NAME" -T "$MODIFIED_FILE" \
        | xz -7 -T0 > "$DELTA_PARTIAL"
    release_work
fi

# -7/-T0 保持原压缩参数；pipefail 确保 tar 或 xz 失败都不发布半成品。
# 所有生成步骤和卸载成功后才将临时产物移到正式路径。
mv -- "$DELTA_PARTIAL" "$DELTA_FILE"

# --- 增量包大小阈值检查 ---
# 如果增量包体积超过全量镜像的 MAX_RATIO%，说明差异太大，增量更新意义不大，跳过
DELTA_SIZE=$(stat -c %s "$DELTA_FILE")
RATIO=$((DELTA_SIZE * 100 / FULL_SIZE))

echo "Delta size: $(numfmt --to=iec "$DELTA_SIZE") ($RATIO% of full image)"

if [ "$RATIO" -gt "$MAX_RATIO" ]; then
    echo "Delta too large ($RATIO% > $MAX_RATIO%), skipping"
    echo "SKIP" > "$OUTPUT_DIR/delta-status.txt"
    rm -f "$DELTA_FILE"
    exit 0
fi

# --- 生成校验和 ---
# awk '{print $1}': sha256sum 输出格式为 "hash  filename"，只取 hash 部分
CHECKSUM=$(sha256sum "$DELTA_FILE" | awk '{print $1}')
echo "$CHECKSUM  $(basename "$DELTA_FILE")" > "$OUTPUT_DIR/delta-sha256sum.txt"

# --- 输出 manifest 片段（供 publish-delta.sh 合并到发布 manifest 中） ---
cat > "$OUTPUT_DIR/delta-entry.json" <<EOF
{
  "from_version": "${BASE_VERSION}",
  "from_tag": "${BASE_TAG}",
  "filename": "${DELTA_FILENAME}",
  "checksum": "sha256:${CHECKSUM}",
  "size": ${DELTA_SIZE},
  "full_size": ${FULL_SIZE},
  "target_meta_hash": "${TARGET_META_HASH}",
  "format": "${DELTA_FORMAT}"
}
EOF

echo "OK" > "$OUTPUT_DIR/delta-status.txt"

echo "=== Delta generation complete ==="
echo "  File:     $DELTA_FILENAME"
echo "  Size:     $(numfmt --to=iec "$DELTA_SIZE")"
echo "  Ratio:    $RATIO%"
echo "  Checksum: $CHECKSUM"
