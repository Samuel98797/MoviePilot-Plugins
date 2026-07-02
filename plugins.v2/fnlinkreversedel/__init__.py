from typing import Any, Dict, List, Tuple
import os
import time
import threading

from app.plugins import _PluginBase
from app.core.event import eventmanager, Event
from app.schemas.types import EventType
from app.helper.downloader import DownloaderHelper
from app.log import logger
from apscheduler.triggers.interval import IntervalTrigger


class FnLinkReverseDel(_PluginBase):
    plugin_name = "硬链接反向删除"
    plugin_desc = "监控硬链接目录，文件删除时同步删除关联种子"
    plugin_icon = "mediasyncdel.png"
    plugin_version = "1.4"
    plugin_author = "Samuel"
    author_url = "https://github.com/jxxghp/MoviePilot-Plugins"
    plugin_config_prefix = "fnlinkreversedel_"
    plugin_order = 50
    auth_level = 1

    _enabled = False
    _monitor_dirs = ""
    _path_mappings = ""
    _exclude_keywords = ""
    _temp_extensions = ['.mp', '.part', '.tmp', '.temp', '.!qB', '.!qb', '.downloading', '.crdownload']
    _delay_delete = 5
    _orphan_scan_interval = 3600
    _watch_thread = None
    _watch_running = False
    _processing_paths = set()
    _processing_lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._monitor_dirs = config.get("monitor_dirs", "")
        self._path_mappings = config.get("path_mappings", "")
        self._exclude_keywords = config.get("exclude_keywords", "")
        self._delay_delete = int(config.get("delay_delete", 5))
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
                "desc": "手动扫描孤儿种子",
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
                                    {"component": "VTextField", "props": {"model": "monitor_dirs", "label": "监控目录(硬链接目录)", "placeholder": "多个目录用换行分隔"}},
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
                                    {"component": "VTextField", "props": {"model": "path_mappings", "label": "路径映射", "placeholder": "源路径 -> 目标路径，如 /video/link -> /downloads"}},
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
            "orphan_scan_interval": 3600,
        }

    def get_page(self) -> List[dict]:
        return [
            {
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "text": "硬链接反向删除插件 - 监控硬链接目录内文件被删除时同步删除关联做种种子"},
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
                "func": self.scan_orphan_torrents,
                "kwargs": {},
            })
        return services

    def stop_service(self):
        self._stop_watcher()

    def _start_watcher(self):
        self._stop_watcher()
        monitor_dirs = [d.strip() for d in self._monitor_dirs.split("\n") if d.strip()]
        if not monitor_dirs:
            logger.warning("[硬链接反向删除] 未配置监控目录")
            return
        for d in monitor_dirs:
            if not os.path.isdir(d):
                logger.warning(f"[硬链接反向删除] 监控目录不存在: {d}")
        self._watch_running = True
        self._watch_thread = threading.Thread(
            target=self._watch_loop,
            args=(monitor_dirs,),
            daemon=True,
            name="FnLinkReverseDel.Watcher"
        )
        self._watch_thread.start()
        logger.info(f"[硬链接反向删除] 目录监控已启动，监控目录: {monitor_dirs}")

    def _stop_watcher(self):
        self._watch_running = False
        if self._watch_thread and self._watch_thread.is_alive():
            self._watch_thread.join(timeout=5)
        logger.info("[硬链接反向删除] 目录监控已停止")

    def _is_temp_file(self, path_str: str) -> bool:
        path_lower = path_str.lower()
        for ext in self._temp_extensions:
            if path_lower.endswith(ext):
                return True
        return False

    def _should_exclude(self, path_str: str) -> bool:
        if self._is_temp_file(path_str):
            return True
        if self._exclude_keywords:
            keywords = [k.strip() for k in self._exclude_keywords.split(",") if k.strip()]
            for keyword in keywords:
                if keyword and keyword in path_str:
                    return True
        return False

    def _is_in_monitor_dirs(self, file_path: str) -> bool:
        monitor_dirs = [d.strip() for d in self._monitor_dirs.split("\n") if d.strip()]
        file_path_norm = os.path.normpath(file_path)
        for monitor_dir in monitor_dirs:
            monitor_dir_norm = os.path.normpath(monitor_dir)
            try:
                if os.path.commonpath([file_path_norm, monitor_dir_norm]) == monitor_dir_norm:
                    return True
            except ValueError:
                continue
        return False

    def _watch_loop(self, monitor_dirs: List[str]):
        try:
            from watchfiles import watch, Change
            from watchfiles.filters import BaseFilter

            class _DeleteFilter(BaseFilter):
                def __init__(self, plugin):
                    self.plugin = plugin
                    super().__init__()

                def __call__(self, change: Change, path: str) -> bool:
                    if change != Change.deleted:
                        return False
                    path_str = str(path)
                    if self.plugin._should_exclude(path_str):
                        return False
                    if not self.plugin._is_in_monitor_dirs(path_str):
                        return False
                    return True

            for changes in watch(*monitor_dirs, watch_filter=_DeleteFilter(self), force_polling=False):
                if not self._watch_running:
                    break
                for change_type, path in changes:
                    if change_type == Change.deleted:
                        path_str = str(path)
                        logger.info(f"[硬链接反向删除] 检测到文件删除: {path_str}")
                        threading.Thread(
                            target=self._async_handle_delete,
                            args=(path_str,),
                            daemon=True
                        ).start()
        except ImportError:
            logger.warning("[硬链接反向删除] watchfiles未安装，将仅使用事件兜底监听")
        except Exception as e:
            logger.error(f"[硬链接反向删除] 目录监控异常: {str(e)}", exc_info=True)

    def _async_handle_delete(self, file_path: str):
        with self._processing_lock:
            if file_path in self._processing_paths:
                logger.debug(f"[硬链接反向删除] 文件正在处理中，跳过: {file_path}")
                return
            self._processing_paths.add(file_path)
        try:
            self.handle_file_delete(file_path)
        finally:
            with self._processing_lock:
                self._processing_paths.discard(file_path)

    @eventmanager.register(EventType.DownloadFileDeleted)
    def on_download_file_deleted(self, event: Event):
        if not self.get_state():
            return
        event_data = event.event_data or {}
        file_path = event_data.get("file_path")
        if file_path:
            logger.info(f"[硬链接反向删除] 系统删除事件触发: {file_path}")
            threading.Thread(
                target=self._async_handle_delete,
                args=(file_path,),
                daemon=True
            ).start()

    @eventmanager.register(EventType.PluginAction)
    def handle_plugin_action(self, event: Event):
        if not self.get_state():
            return
        event_data = event.event_data or {}
        if event_data.get("action") == "fnlink_scan":
            self.scan_orphan_torrents()

    def handle_file_delete(self, file_path: str):
        if not self.get_state():
            return
        if not self._is_in_monitor_dirs(file_path):
            logger.debug(f"[硬链接反向删除] 文件不在监控目录内，跳过: {file_path}")
            return
        if self._delay_delete > 0:
            for _ in range(3):
                time.sleep(self._delay_delete / 3)
                if os.path.exists(file_path):
                    logger.info(f"[硬链接反向删除] 文件已恢复，跳过删除: {file_path}")
                    return
        if os.path.exists(file_path):
            logger.info(f"[硬链接反向删除] 文件仍存在，跳过删除: {file_path}")
            return
        self.handle_torrent(file_path)

    def _path_matches(self, tf_path: str, target_path: str) -> bool:
        if not tf_path or not target_path:
            return False
        tf_norm = os.path.normpath(tf_path)
        target_norm = os.path.normpath(target_path)
        if tf_norm == target_norm:
            return True
        target_with_sep = target_norm if target_norm.endswith(os.sep) else target_norm + os.sep
        if tf_norm.startswith(target_with_sep):
            return True
        tf_parent = os.path.dirname(tf_norm)
        if tf_parent == target_norm:
            return True
        return False

    def handle_torrent(self, file_path: str):
        logger.info(f"[硬链接反向删除] 处理文件删除: {file_path}")
        mapped_path = self._map_path(file_path)

        downloader_helper = DownloaderHelper()
        try:
            downloader = downloader_helper.get_service()
            if not downloader:
                logger.warning("[硬链接反向删除] 未找到启用的下载器")
                return
            torrents = downloader.instance.get_torrents()
        except Exception as e:
            logger.error(f"[硬链接反向删除] 获取种子列表失败: {str(e)}")
            return

        deleted_hashes = set()
        for torrent in torrents:
            try:
                torrent_hash = getattr(torrent, 'hash', '') or getattr(torrent, 'hashString', '')
                if not torrent_hash or torrent_hash in deleted_hashes:
                    continue
                torrent_name = getattr(torrent, 'name', str(torrent_hash))
                torrent_files = getattr(torrent, 'files', []) or []
                file_paths = []
                for tf in torrent_files:
                    if hasattr(tf, 'path'):
                        file_paths.append(str(tf.path))
                    elif hasattr(tf, 'name'):
                        file_paths.append(str(tf.name))
                    else:
                        file_paths.append(str(tf))
                if not file_paths:
                    torrent_path = getattr(torrent, 'path', '') or getattr(torrent, 'save_path', '')
                    if torrent_path:
                        file_paths = [str(torrent_path)]
                match_found = False
                for tf_path in file_paths:
                    if not tf_path:
                        continue
                    if (self._path_matches(tf_path, file_path) or
                            self._path_matches(tf_path, mapped_path)):
                        match_found = True
                        break
                if match_found:
                    deleted_hashes.add(torrent_hash)
                    logger.info(f"[硬链接反向删除] 找到关联种子: {torrent_name} ({torrent_hash})")
                    try:
                        downloader.instance.delete_torrent(hash=torrent_hash, delete_files=True)
                        logger.info(f"[硬链接反向删除] 已删除种子及文件: {torrent_name}")
                    except Exception as e:
                        logger.error(f"[硬链接反向删除] 删除种子失败: {torrent_name}, 错误: {str(e)}")
            except Exception as e:
                logger.error(f"[硬链接反向删除] 处理种子失败: {str(e)}")

        if not deleted_hashes:
            logger.info(f"[硬链接反向删除] 未找到关联种子: {file_path}")

    def _map_path(self, file_path: str) -> str:
        if not self._path_mappings:
            return file_path
        for mapping in self._path_mappings.split("\n"):
            mapping = mapping.strip()
            if "->" in mapping:
                src, dst = mapping.split("->", 1)
                src = src.strip()
                dst = dst.strip()
                if src and file_path.startswith(src):
                    return file_path.replace(src, dst, 1)
        return file_path

    def scan_orphan_torrents(self):
        if not self.get_state():
            return
        monitor_dirs = [d.strip() for d in self._monitor_dirs.split("\n") if d.strip()]
        if not monitor_dirs:
            return

        logger.info("[硬链接反向删除] 开始扫描孤儿种子")
        downloader_helper = DownloaderHelper()
        try:
            downloader = downloader_helper.get_service()
            if not downloader:
                logger.warning("[硬链接反向删除] 未找到启用的下载器")
                return
            torrents = downloader.instance.get_torrents()
        except Exception as e:
            logger.error(f"[硬链接反向删除] 获取种子列表失败: {str(e)}")
            return

        deleted_count = 0
        for torrent in torrents:
            try:
                torrent_hash = getattr(torrent, 'hash', '') or getattr(torrent, 'hashString', '')
                if not torrent_hash:
                    continue
                torrent_name = getattr(torrent, 'name', str(torrent_hash))
                torrent_files = getattr(torrent, 'files', []) or []
                file_paths = []
                for tf in torrent_files:
                    if hasattr(tf, 'path'):
                        file_paths.append(str(tf.path))
                    elif hasattr(tf, 'name'):
                        file_paths.append(str(tf.name))
                    else:
                        file_paths.append(str(tf))
                if not file_paths:
                    torrent_path = getattr(torrent, 'path', '') or getattr(torrent, 'save_path', '')
                    if torrent_path:
                        file_paths = [str(torrent_path)]
                if not file_paths:
                    continue
                all_missing = True
                has_monitored_file = False
                for tf_path in file_paths:
                    if not tf_path:
                        continue
                    tf_path_mapped = self._map_path(tf_path)
                    is_monitored = self._is_in_monitor_dirs(tf_path) or self._is_in_monitor_dirs(tf_path_mapped)
                    if is_monitored:
                        has_monitored_file = True
                    if os.path.exists(tf_path) or (tf_path_mapped != tf_path and os.path.exists(tf_path_mapped)):
                        all_missing = False
                        break
                if all_missing and has_monitored_file:
                    try:
                        downloader.instance.delete_torrent(hash=torrent_hash, delete_files=True)
                        deleted_count += 1
                        logger.info(f"[硬链接反向删除] 删除孤儿种子: {torrent_name}")
                    except Exception as e:
                        logger.error(f"[硬链接反向删除] 删除孤儿种子失败: {torrent_name}, 错误: {str(e)}")
            except Exception as e:
                logger.error(f"[硬链接反向删除] 处理孤儿种子失败: {str(e)}")

        logger.info(f"[硬链接反向删除] 孤儿种子扫描完成，共删除 {deleted_count} 个")
