# 大 QMT 临时粘贴入口

这个文件用于 QMT「模型交易」里只能粘贴代码、不能导入文件的情况。

## 操作

1. 在「模型交易」中新建一个 Python 模型或 Python 策略。
2. 把下面代码完整粘贴进去。
3. 保存模型，但先不要点击启动。
4. 运行模式选择「实盘」；不要选择「模拟」或「独立 Python 进程」。
5. 启动后查看输出面板，应该出现 `bigqmt`、`started`、`zmq` 等字样。

## 粘贴代码

```python
# coding:gbk
"""QMT 模型交易入口：加载同目录的完整大 QMT ZMQ 桥接入口。"""
import datetime
import os
import sys
import traceback

QMT_PYTHON_DIR = r"C:\国金证券QMT交易端\python"
if os.path.isdir(QMT_PYTHON_DIR) and QMT_PYTHON_DIR not in sys.path:
    sys.path.insert(0, QMT_PYTHON_DIR)


def _find_bridge_entry():
    candidates = []
    entry_file = globals().get("__file__")
    if entry_file:
        candidates.append(os.path.dirname(os.path.abspath(entry_file)))
    cwd = os.getcwd()
    if cwd and cwd not in candidates:
        candidates.append(cwd)
    for path in sys.path:
        if path and path not in candidates:
            candidates.append(path)
    for directory in candidates:
        entry = os.path.join(directory, "BIGQMT_REDIS_DRYRUN.py")
        if os.path.isfile(entry):
            return entry
    raise ImportError(
        "BIGQMT_REDIS_DRYRUN.py was not found beside the model or on sys.path"
    )


def _write_bootstrap_error(entry_path):
    try:
        log_dir = os.path.join(os.path.dirname(entry_path), "logs")
        if not os.path.isdir(log_dir):
            os.makedirs(log_dir)
        log_path = os.path.join(log_dir, "bigqmt-bootstrap-error.log")
        with open(log_path, "a") as log_file:
            log_file.write(
                "\n%s BIGQMT_ZMQ_DRYRUN bootstrap failed\n"
                % datetime.datetime.now().isoformat()
            )
            traceback.print_exc(file=log_file)
        print("[bigqmt_shell] bootstrap traceback written to %s" % log_path)
    except Exception as log_error:
        print("[bigqmt_shell] bootstrap traceback could not be written: %s" % log_error)


BIGQMT_FORCE_TRANSPORT = "zmq"
try:
    _BRIDGE_ENTRY = _find_bridge_entry()
    with open(_BRIDGE_ENTRY, "rb") as source_file:
        _BRIDGE_SOURCE = source_file.read()
    exec(compile(_BRIDGE_SOURCE, _BRIDGE_ENTRY, "exec"), globals(), globals())
except Exception:
    _bootstrap_log_anchor = (
        globals().get("_BRIDGE_ENTRY")
        or globals().get("__file__")
        or os.path.join(os.getcwd(), "BIGQMT_ZMQ_DRYRUN.py")
    )
    _write_bootstrap_error(_bootstrap_log_anchor)
    raise
```

## 失败时

如果提示找不到 `BIGQMT_REDIS_DRYRUN.py`，说明模型编辑器执行时的目录不在 QMT 的 `python` 目录。不要反复启动，把输出面板最后 20 行发回来即可。

如果出现 `download globals bound=[]`，说明 QMT 没有以模型交易的实盘上下文执行代码，先停止该模型，不要继续测试交易接口。

当前桥接配置是只读模式：远程下单和撤单关闭。
