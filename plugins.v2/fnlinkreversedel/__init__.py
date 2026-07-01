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
    """
    硬链接反向删除插件
    监控目录内文件被删除时，同步删除关联的种子和历史记录
    """
    plugin_name = "硬链接反向删除"
    plugin_desc = "监控硬链接目录，文件删除时同步删除关联种子和历史记录"
    plugin_icon = "mediasyncdel.png"
    plugin_version = "1.0"
    plugin_author = "Samuel"
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
        """根据当前配置初始化插件"""
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
        """返回插件当前是否启用"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令"""
        return [
            {
                "cmd": "/fnlink_scan",
                "event": EventType.PluginAction,
                "desc": "手动扫描孤儿文件",
                "category": "插件命令",
                "data": {
                    "action": "fnlink_scan",
                },
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """注册插件API"""
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回配置页JSON和默认配置模型"""
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
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "delay_delete",
                                            "label": "延迟删除(秒)",
                                            "type": "number",
                                            "min": 0,
                                            "max": 60,
                                        },
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
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "monitor_dirs",
                                            "label": "监控目录",
                                            "placeholder": "多个目录用换行分隔，如：/vol2/1000/影视/电影",
                                        },
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
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "path_mappings",
                                            "label": "路径映射",
                                            "placeholder": "多行配置，格式：源路径 -> 目标路径，如：/vol2/1000/影视 -> /downloads/complete",
                                        },
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
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "exclude_keywords",
                                            "label": "排除关键字",
                                            "placeholder": "多个关键字用逗号分隔",
                                        },
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "clear_history",
                                            "label": "清理历史记录",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "orphan_scan_interval",
                                            "label": "孤儿扫描间隔(秒)",
                                            "type": "number",
                                            "min": 60,
                                            "max": 86400,
                                        },
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
        """返回详情页JSON"""
        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info",
                    "variant": "tonal",
                    "text": "硬链接反向删除插件 - 监控目录内文件被删除时，同步删除关联的种子和历史记录",
                },
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """注册定时任务"""
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
        """停用插件时清理后台任务"""
        self._stop_watcher()

    def _start_watcher(self):
        """启动目录监控"""
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
        """停止目录监控"""
        self._watch_running = False
        if self._watch_thread and self._watch_thread.is_alive():
            self._watch_thread.join(timeout=5)
        logger.info("[硬链接反向删除] 目录监控已停止")

    def _watch_loop(self, monitor_dirs: List[str]):
        """目录监控循环"""
        try:
            from watchfiles import watch
            for changes in watch(*monitor_dirs, watch_filter=self._watch_filter):
                if not self._watch_running:
                    break
                for change_type, path in changes:
                    if change_type.name == "deleted":
                        logger.info(f"[硬链接反向删除] 检测到文件删除: {path}")
                        threading.Thread(
                            target=self.handle_file_delete,
                            args=(str(path),),
                            daemon=True
                        ).start()
        except ImportError:
            logger.warning("[硬链接反向删除] watchfiles未安装，将使用兜底监听")
        except Exception as e:
            logger.error(f"[硬链接反向删除] 目录监控异常: {str(e)}")

    def _watch_filter(self, change_type, path: str) -> bool:
        """监控过滤器"""
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
        """监听下载文件删除事件（兜底）"""
        if not self.get_state():
            return
        event_data = event.event_data or {}
        file_path = event_data.get("file_path")
        if file_path:
            logger.info(f"[硬链接反向删除] 兜底事件触发: {file_path}")
            threading.Thread(
                target=self.handle_file_delete,
                args=(file_path,),
                daemon=True
            ).start()

    @eventmanager.register(EventType.PluginAction)
    def handle_plugin_action(self, event: Event):
        """处理插件命令"""
        if not self.get_state():
            return
        event_data = event.event_data or {}
        if event_data.get("action") == "fnlink_scan":
            self.scan_orphan_files()

    def handle_file_delete(self, file_path: str):
        """处理文件删除"""
        if not self.get_state():
            return

        if self._delay_delete > 0:
            time.sleep(self._delay_delete)

        if os.path.exists(file_path):
            return

        self.handle_torrent(file_path)

    def handle_torrent(self, file_path: str):
        """处理种子关联删除"""
        logger.info(f"[硬链接反向删除] 处理文件删除: {file_path}")

        mapped_path = self._map_path(file_path)

        history_oper = DownloadHistoryOper()
        try:
            hashs = history_oper.get_hashs_by_fullpath(mapped_path)
        except Exception as e:
            logger.error(f"[硬链接反向删除] 获取hash失败: {str(e)}")
            hashs = []

        if not hashs:
            try:
                hashs = history_oper.get_hashs_by_fullpath(file_path)
            except Exception as e:
                logger.error(f"[硬链接反向删除] 获取hash失败: {str(e)}")
                hashs = []

        if not hashs:
            logger.info(f"[硬链接反向删除] 未找到文件关联的下载记录: {file_path}")
            return

        for hash_value in hashs:
            self.__del_seed(hash_value, file_path)

    def _map_path(self, file_path: str) -> str:
        """路径映射转换"""
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
        """删除种子"""
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
                    downloader.delete_torrent(
                        hash=hash_value,
                        delete_files=True
                    )
                    deleted = True
                    logger.info(f"[硬链接反向删除] 已从下载器 {downloader.name} 删除种子: {torrent.name}")
            except Exception as e:
                logger.error(f"[硬链接反向删除] 删除种子失败: {str(e)}")

        if not deleted:
            logger.warning(f"[硬链接反向删除] 下载器中未找到种子: {hash_value}")

        if self._clear_history:
            self.__del_collection(hash_value)

    def __del_collection(self, hash_value: str):
        """删除整理历史记录"""
        logger.info(f"[硬链接反向删除] 删除整理历史: {hash_value}")

        history_oper = DownloadHistoryOper()
        try:
            history_records = history_oper.get_files_by_hash(hash_value)
        except Exception as e:
            logger.error(f"[硬链接反向删除] 获取历史记录失败: {str(e)}")
            return

        for record in history_records:
            try:
                history_oper.delete_history(record.id)
                logger.info(f"[硬链接反向删除] 已删除历史记录: {record.filepath}")
            except Exception as e:
                logger.error(f"[硬链接反向删除] 删除历史记录失败: {str(e)}")

    def scan_orphan_files(self):
        """扫描孤儿文件"""
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