#!/bin/bash
# 兼容旧路径: 等同于工程根目录的 ./convert.sh
exec "$(dirname "$0")/../convert.sh" "$@"
