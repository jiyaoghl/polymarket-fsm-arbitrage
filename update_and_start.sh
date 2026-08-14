#!/bin/bash
# 转发调用统一的 VPS 脚本
cd "$(dirname "$0")" || exit 1
bash vps.sh update

