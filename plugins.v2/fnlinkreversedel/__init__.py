from typing import Any, Dict, List, Tuple
import os
import time
import threading

from app.plugins import _PluginBase
from app.core.event import eventmanager, Event
from app.schemas.types import EventType
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.db.downloadhistory_oper import DownloadHistoryOper
from apscheduler.triggers.interval import IntervalTrigger


class FnLinkReverseDel(_PluginBase):
    plugin_name = "硬链接反向删除"
    plugin_desc = "监控硬链接目录，文件删除时同步删除关联种子和历史记录"
    plugin_icon = "mediasyncdel.png"
    plugin_version = "1.2"
    plugin_author = "Samuel"
    author_url = "https://github.com/jxxghp/MoviePilot-Plugins"
    plugin_config_prefix = "fnlinkreversedel_"
    plugin_order = 50
    auth_level = 1

    _enabled = False
    _monitor_dirs = ""
    _path_mappings = ""
    _exclude_keywords = ""
    _delay_delete = 5
    _clear_history = True
    _orphan_scan_interval = 3600
    _watch_thread = None
    _watch_running = False

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._monitor_dirs = config.get("monitor_dirs", "")
        self._path_mappings = config.get("path_mappings", "")
        self._exclude_keywords = config.get("exclude_keywords", "")
        self._delay_delete = int(config.get("delay_delete", 5))
        self._clear_history = bool(config.get("clear_history", True))
        self._orphan_scan_interval = int(config.get("orphan_scan_interval", 3600))
        if self._enabled:
            self._start_watcher()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/fnlink_scan",
                "event": EventType.PluginAction,
                "desc": "手动扫描孤儿文件",
                "category": "插件命令",
                "data": {"action": "fnlink_scan"},
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "delay_delete", "label": "延迟删除(秒)", "type": "number", "min": 0, "max": 60},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {"component": "VTextField", "props": {"model": "monitor_dirs", "label": "监控目录", "placeholder": "多个目录用换行分隔"}},
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {"component": "VTextField", "props": {"model": "path_mappings", "label": "路径映射", "placeholder": "源路径 -> 目标路径"}},
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {"component": "VTextField", "props": {"model": "exclude_keywords", "label": "排除关键字", "placeholder": "逗号分隔"}},
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "clear_history", "label": "清理历史记录"}},
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {"model": "orphan_scan_interval", "label": "孤儿扫描间隔(秒)", "type": "number", "min": 60, "max": 86400},
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "monitor_dirs": "",
            "path_mappings": "",
            "exclude_keywords": "",
            "delay_delete": 5,
            "clear_history": True,
            "orphan_scan_interval": 3600,
        }

    def get_page(self) -> List[dict]:
        return [
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "text": "硬链接反向删除插件 - 监控目录内文件被删除时同步删除关联种子和历史记录"},
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        if not self.get_state():
            return []
        services = []
        if self._orphan_scan_interval > 0:
            services.append({
                "id": "FnLinkReverseDel.OrphanScan",
                "name": "硬链接反向删除-孤儿扫描",
                "trigger": IntervalTrigger(seconds=self._orphan_scan_interval),
                "func": self.scan_orphan_files,
                "kwargs": {},
            })
        return services

    def stop_service(self):
        self._stop_watcher()

    def _start_watcher(self):
        self._stop_watcher()
        monitor_dirs = [d.strip() for d in self._monitor_dirs.split("\n") if d.strip()]
        if not monitor_dirs:
            return
        self._watch_running = True
        self._watch_thread = threading.Thread(
            target=self._watch_loop,
            args=(monitor_dirs,),
            daemon=True,
            name="FnLinkReverseDel.Watcher"
        )
        self._watch_thread.start()
        logger.info("[硬链接反向删除] 目录监控已启动")

    def _stop_watcher(self):
        self._watch_running = False
        if self._watch_thread and self._watch_thread.is_alive():
            self._watch_thread.join(timeout=5)
        logger.info("[硬链接反向删除] 目录监控已停止")

    def _watch_loop(self, monitor_dirs: List[str]):
        try:
            from watchfiles import watch
            for changes in watch(*monitor_dirs, watch_filter=self._watch_filter):
                if not self._watch_running:
                    break
                for change_type, path in changes:
                    if change_type.name == "deleted":
                        logger.info(f"[硬链接反向删除] 检测到文件删除: {path}")
                        threading.Thread(target=self.handle_file_delete, args=(str(path),), daemon=True).start()
        except ImportError:
            logger.warning("[硬链接反向删除] watchfiles未安装，将使用兜底监听")
        except Exception as e:
            logger.error(f"[硬链接反向删除] 目录监控异常: {str(e)}")

    def _watch_filter(self, change_type, path: str) -> bool:
        path_str = str(path)
        if change_type.name == "deleted":
            if self._exclude_keywords:
                keywords = [k.strip() for k in self._exclude_keywords.split(",") if k.strip()]
                for keyword in keywords:
                    if keyword in path_str:
                        return False
            return True
        return False

    @eventmanager.register(EventType.DownloadFileDeleted)
    def on_download_file_deleted(self, event: Event):
        if not self.get_state():
            return
        event_data = event.event_data or {}
        file_path = event_data.get("file_path")
        if file_path:
            logger.info(f"[硬链接反向删除] 兜底事件触发: {file_path}")
            threading.Thread(target=self.handle_file_delete, args=(file_path,), daemon=True).start()

    @eventmanager.register(EventType.PluginAction)
    def handle_plugin_action(self, event: Event):
        if not self.get_state():
            return
        event_data = event.event_data or {}
        if event_data.get("action") == "fnlink_scan":
            self.scan_orphan_files()

    def handle_file_delete(self, file_path: str):
        if not self.get_state():
            return
        if self._delay_delete > 0:
            time.sleep(self._delay_delete)
        if os.path.exists(file_path):
            return
        self.handle_torrent(file_path)

    def handle_torrent(self, file_path: str):
        logger.info(f"[硬链接反向删除] 处理文件删除: {file_path}")
        mapped_path = self._map_path(file_path)

        history_oper = DownloadHistoryOper()
        try:
            all_history = history_oper.list_history()
        except Exception as e:
            logger.error(f"[硬链接反向删除] 获取历史记录失败: {str(e)}")
            return

        hashs = set()
        for record in all_history:
            if not record.filepath:
                continue
            record_path = record.filepath

            if record_path == file_path or record_path == mapped_path:
                if record.hash:
                    hashs.add(record.hash)
                    logger.info(f"[硬链接反向删除] 找到匹配记录: {record_path} -> {record.hash}")
                continue

            if file_path.endswith(os.sep):
                if record_path.startswith(file_path):
                    if record.hash:
                        hashs.add(record.hash)
                        logger.info(f"[硬链接反向删除] 找到目录匹配记录: {record_path} -> {record.hash}")
                elif mapped_path.endswith(os.sep) and record_path.startswith(mapped_path):
                    if record.hash:
                        hashs.add(record.hash)
                        logger.info(f"[硬链接反向删除] 找到映射目录匹配记录: {record_path} -> {record.hash}")
                continue

            record_dir = os.path.dirname(record_path)
            if record_dir == file_path or record_dir == mapped_path:
                if record.hash:
                    hashs.add(record.hash)
                    logger.info(f"[硬链接反向删除] 找到父目录匹配记录: {record_path} -> {record.hash}")
                continue

            if file_path in record_path or mapped_path in record_path:
                if record.hash:
                    hashs.add(record.hash)
                    logger.info(f"[硬链接反向删除] 找到包含匹配记录: {record_path} -> {record.hash}")
                continue

        if not hashs:
            logger.info(f"[硬链接反向删除] 未找到文件关联的下载记录: {file_path}")
            return

        for hash_value in hashs:
            self.__del_seed(hash_value, file_path)

    def _map_path(self, file_path: str) -> str:
        if not self._path_mappings:
            return file_path
        for mapping in self._path_mappings.split("\n"):
            mapping = mapping.strip()
            if "->" in mapping:
                src, dst = mapping.split("->", 1)
                src = src.strip()
                dst = dst.strip()
                if file_path.startswith(src):
                    return file_path.replace(src, dst, 1)
        return file_path

    def __del_seed(self, hash_value: str, file_path: str):
        logger.info(f"[硬链接反向删除] 删除种子: {hash_value}")
        downloader_helper = DownloaderHelper()
        try:
            downloaders = downloader_helper.get_enabled_downloaders()
        except Exception as e:
            logger.error(f"[硬链接反向删除] 获取下载器失败: {str(e)}")
            return

        deleted = False
        for downloader in downloaders:
            try:
                torrents = downloader.get_torrents(hash=hash_value)
                for torrent in torrents:
                    downloader.delete_torrent(hash=hash_value, delete_files=True)
                    deleted = True
                    logger.info(f"[硬链接反向删除] 已从下载器 {downloader.name} 删除种子: {torrent.name}")
            except Exception as e:
                logger.error(f"[硬链接反向删除] 删除种子失败: {str(e)}")

        if not deleted:
            logger.warning(f"[硬链接反向删除] 下载器中未找到种子: {hash_value}")

        if self._clear_history:
            self.__del_collection(hash_value)

    def __del_collection(self, hash_value: str):
        logger.info(f"[硬链接反向删除] 删除整理历史: {hash_value}")
        history_oper = DownloadHistoryOper()
        try:
            all_history = history_oper.list_history()
        except Exception as e:
            logger.error(f"[硬链接反向删除] 获取历史记录失败: {str(e)}")
            return

        for record in all_history:
            if record.hash == hash_value:
                try:
                    history_oper.delete_history(record.id)
                    logger.info(f"[硬链接反向删除] 已删除历史记录: {record.filepath}")
                except Exception as e:
                    logger.error(f"[硬链接反向删除] 删除历史记录失败: {str(e)}")

    def scan_orphan_files(self):
        if not self.get_state():
            return
        if not self._monitor_dirs:
            return
        monitor_dirs = [d.strip() for d in self._monitor_dirs.split("\n") if d.strip()]
        if not monitor_dirs:
            return

        logger.info("[硬链接反向删除] 开始扫描孤儿文件")
        history_oper = DownloadHistoryOper()
        try:
            all_history = history_oper.list_history()
        except Exception as e:
            logger.error(f"[硬链接反向删除] 获取历史记录失败: {str(e)}")
            return

        for record in all_history:
            if not record.filepath:
                continue
            full_path = record.filepath
            if not os.path.exists(full_path):
                logger.info(f"[硬链接反向删除] 发现孤儿文件: {full_path}")
                self.handle_torrent(full_path)

        logger.info("[硬链接反向删除] 孤儿文件扫描完成")