import os
import time
import threading
from typing import Any, Dict, List, Tuple, Optional

from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType


class FnLinkReverseDel(_PluginBase):
    plugin_name = "硬链接反向删除"
    plugin_desc = "监控硬链接目录，文件删除时同步删除关联种子"
    plugin_icon = "mediasyncdel.png"
    plugin_version = "2.1"
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
    _orphan_scan_interval = 3600
    _watch_thread = None
    _watch_running = False
    _processing_paths = set()
    _processing_lock = threading.Lock()
    _recent_processed = {}
    _scheduler = None
    _transferhis = None
    _downloadhis = None
    _downloader_helper = None
    _default_downloader = None

    @staticmethod
    def _safe_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def init_plugin(self, config: dict = None):
        self.stop_service()
        self._transferhis = TransferHistoryOper()
        self._downloadhis = DownloadHistoryOper()
        self._downloader_helper = DownloaderHelper()
        try:
            self._default_downloader = None
            downloader_services = self._downloader_helper.get_services()
            if downloader_services:
                for downloader_name, downloader_info in downloader_services.items():
                    if hasattr(downloader_info, 'config') and downloader_info.config and getattr(downloader_info.config, 'default', False):
                        self._default_downloader = downloader_name
                        break
                if not self._default_downloader:
                    first_key = next(iter(downloader_services), None)
                    if first_key:
                        self._default_downloader = first_key
        except Exception as e:
            logger.debug(f"[硬链接反向删除] 获取默认下载器失败: {str(e)}")

        if config:
            self._enabled = bool(config.get("enabled"))
            self._monitor_dirs = config.get("monitor_dirs") or ""
            self._path_mappings = config.get("path_mappings") or ""
            self._exclude_keywords = config.get("exclude_keywords") or ""
            self._delay_delete = self._safe_int(config.get("delay_delete"), 5)
            self._orphan_scan_interval = self._safe_int(config.get("orphan_scan_interval"), 3600)
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
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'enabled', 'label': '启用插件'}}
                                ],
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 8},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'delay_delete',
                                            'label': '延迟删除(秒)',
                                            'type': 'number',
                                            'placeholder': '5'
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'monitor_dirs',
                                            'label': '监控目录(硬链接目录/媒体库目录)',
                                            'rows': '3',
                                            'placeholder': '多个目录用换行分隔，如：/video/link'
                                        }
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'path_mappings',
                                            'label': '路径映射(媒体服务器路径:MoviePilot路径)',
                                            'rows': '2',
                                            'placeholder': '硬链接路径 -> 下载源路径，如：/video/link -> /downloads\n若媒体服务器路径与MoviePilot路径不同，用冒号分隔，如：/data/link:/video/link'
                                        }
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'exclude_keywords',
                                            'label': '排除关键字',
                                            'placeholder': '逗号分隔'
                                        }
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 8},
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'orphan_scan_interval',
                                            'label': '孤儿扫描间隔(秒)',
                                            'type': 'number',
                                            'placeholder': '3600'
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '注意：监控目录必须配置为硬链接目录（媒体库目录），不是下载源目录。路径映射用于将硬链接路径转换为下载器内的源文件路径，路径一致可不填。参考mediasyncdel实现，依赖MoviePilot转移历史记录匹配种子，刮削重命名也能正确识别。'
                                        }
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
        logs = self.get_data('logs') or []
        if not logs:
            return [
                {
                    'component': 'VAlert',
                    'props': {
                        'type': 'info',
                        'variant': 'tonal',
                        'text': '硬链接反向删除插件 - 监控硬链接目录内文件被删除时同步删除关联做种种子。暂无操作记录。'
                    }
                }
            ]
        logs = sorted(logs, key=lambda x: x.get('time', ''), reverse=True)[:20]
        contents = []
        for log_item in logs:
            contents.append({
                'component': 'div',
                'props': {'class': 'd-flex align-center pa-2 border-b'},
                'content': [
                    {
                        'component': 'div',
                        'props': {'class': 'text-caption text-grey mr-3 flex-no-shrink'},
                        'text': log_item.get('time', '')
                    },
                    {
                        'component': 'div',
                        'props': {'class': 'text-body-2 text-truncate'},
                        'text': log_item.get('message', '')
                    }
                ]
            })
        return [
            {
                'component': 'VAlert',
                'props': {
                    'type': 'info',
                    'variant': 'tonal',
                    'text': '硬链接反向删除插件 - 最近20条操作记录'
                }
            },
            {
                'component': 'div',
                'props': {'class': 'mt-3'},
                'content': contents
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
        if self._scheduler:
            try:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
            except Exception:
                pass
            self._scheduler = None

    def _log_action(self, message: str):
        try:
            logs = self.get_data('logs') or []
            logs.append({
                'time': time.strftime('%Y-%m-%d %H:%M:%S'),
                'message': message
            })
            logs = logs[-100:]
            self.save_data('logs', logs)
        except Exception:
            pass

    def _parse_monitor_dirs(self) -> List[str]:
        """解析监控目录，自动处理误填的路径映射格式"""
        monitor_dirs = []
        auto_mappings = []
        for line in self._monitor_dirs.split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = None
            for sep in ['->', '：', ':']:
                if sep in line:
                    candidate = line.split(sep, 1)
                    if len(candidate) == 2 and candidate[0].strip().startswith('/'):
                        parts = candidate
                        break
            if parts and len(parts) == 2:
                monitor_dir = parts[0].strip()
                src_dir = parts[1].strip()
                monitor_dirs.append(monitor_dir)
                if monitor_dir != src_dir:
                    auto_mappings.append(f"{monitor_dir}->{src_dir}")
            else:
                monitor_dirs.append(line)
        if auto_mappings and not self._path_mappings:
            self._path_mappings = "\n".join(auto_mappings)
            logger.info(f"[硬链接反向删除] 自动从监控目录提取路径映射: {auto_mappings}")
        return monitor_dirs

    def _start_watcher(self):
        self._stop_watcher()
        monitor_dirs = self._parse_monitor_dirs()
        if not monitor_dirs:
            logger.warning("[硬链接反向删除] 未配置监控目录")
            return
        valid_dirs = []
        for d in monitor_dirs:
            if not os.path.isdir(d):
                logger.warning(f"[硬链接反向删除] 监控目录不存在: {d}")
            else:
                valid_dirs.append(d)
        if not valid_dirs:
            logger.error("[硬链接反向删除] 没有有效的监控目录，监控未启动")
            return
        self._watch_running = True
        self._watch_thread = threading.Thread(
            target=self._watch_loop,
            args=(valid_dirs,),
            daemon=True,
            name="FnLinkReverseDel.Watcher"
        )
        self._watch_thread.start()
        logger.info(f"[硬链接反向删除] 目录监控已启动，监控目录: {valid_dirs}")

    def _stop_watcher(self):
        self._watch_running = False
        if self._watch_thread and self._watch_thread.is_alive():
            self._watch_thread.join(timeout=5)
        self._watch_thread = None

    @staticmethod
    def _normalize_path(path_str: str) -> str:
        return str(path_str).replace('\\', '/').rstrip('/')

    @classmethod
    def _parent_dir(cls, path_str: str) -> str:
        norm = cls._normalize_path(path_str)
        idx = norm.rfind('/')
        if idx <= 0:
            return norm
        return norm[:idx]

    def _is_media_file(self, path_str: str) -> bool:
        return os.path.splitext(str(path_str).lower())[1] in settings.RMT_MEDIAEXT

    def _is_temp_file(self, path_str: str) -> bool:
        temp_extensions = ['.mp', '.part', '.tmp', '.temp', '.!qB', '.!qb', '.downloading', '.crdownload']
        path_lower = path_str.lower()
        for ext in temp_extensions:
            if path_lower.endswith(ext):
                return True
        return False

    def _should_exclude(self, path_str: str) -> bool:
        if self._is_temp_file(path_str):
            return True
        if not self._is_media_file(path_str):
            return True
        if self._exclude_keywords:
            keywords = [k.strip() for k in self._exclude_keywords.split(",") if k.strip()]
            for keyword in keywords:
                if keyword and keyword in path_str:
                    return True
        return False

    def _is_in_monitor_dirs(self, file_path: str) -> bool:
        monitor_dirs = self._parse_monitor_dirs()
        file_path_norm = self._normalize_path(os.path.normpath(file_path))
        for monitor_dir in monitor_dirs:
            monitor_dir_norm = self._normalize_path(os.path.normpath(monitor_dir))
            if file_path_norm == monitor_dir_norm:
                return True
            if file_path_norm.startswith(monitor_dir_norm + "/"):
                return True
        return False

    def _is_recently_processed(self, file_path: str, window: int = 10) -> bool:
        now = time.time()
        path_norm = self._normalize_path(file_path)
        with self._processing_lock:
            expired_keys = [k for k, v in self._recent_processed.items() if now - v > window]
            for k in expired_keys:
                del self._recent_processed[k]
            if path_norm in self._recent_processed:
                return True
            for processed_path in list(self._recent_processed.keys()):
                if path_norm.startswith(processed_path + "/") or processed_path.startswith(path_norm + "/"):
                    return True
            self._recent_processed[path_norm] = now
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

            normalized_dirs = []
            for d in monitor_dirs:
                normalized_dirs.append(self._normalize_path(os.path.normpath(d)))

            for changes in watch(*normalized_dirs, watch_filter=_DeleteFilter(self), force_polling=False):
                if not self._watch_running:
                    break
                for change_type, path in changes:
                    if change_type == Change.deleted:
                        path_str = str(path)
                        path_norm = self._normalize_path(path_str)
                        logger.info(f"[硬链接反向删除] 检测到文件删除: {path_norm}")
                        threading.Thread(
                            target=self._async_handle_delete,
                            args=(path_norm,),
                            daemon=True
                        ).start()
        except ImportError:
            logger.warning("[硬链接反向删除] watchfiles未安装，将仅使用系统事件兜底监听")
        except Exception as e:
            logger.error(f"[硬链接反向删除] 目录监控异常: {str(e)}", exc_info=True)

    def _async_handle_delete(self, file_path: str):
        if self._is_recently_processed(file_path, window=60):
            logger.debug(f"[硬链接反向删除] 重复事件，已跳过: {file_path}")
            return
        with self._processing_lock:
            if file_path in self._processing_paths:
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
        file_path = event_data.get("file_path") or event_data.get("src")
        if file_path:
            file_norm = self._normalize_path(file_path)
            if not self._is_media_file(file_norm):
                return
            logger.info(f"[硬链接反向删除] 系统删除事件触发: {file_norm}")
            threading.Thread(
                target=self._async_handle_delete,
                args=(file_norm,),
                daemon=True
            ).start()

    @eventmanager.register(EventType.PluginAction)
    def handle_plugin_action(self, event: Event):
        if not self.get_state():
            return
        event_data = event.event_data or {}
        if event_data.get("action") == "fnlink_scan":
            self.scan_orphan_torrents()

    def _map_path(self, path_str: str, direction: str = "to_src") -> str:
        path_norm = self._normalize_path(path_str)
        if not self._path_mappings:
            return path_norm
        for mapping in self._path_mappings.split("\n"):
            mapping = mapping.strip()
            if not mapping:
                continue
            if "->" in mapping:
                parts = mapping.split("->", 1)
            elif "：" in mapping:
                parts = mapping.split("：", 1)
            elif ":" in mapping and "/" in mapping.split(":", 1)[0]:
                parts = mapping.split(":", 1)
            else:
                continue
            if len(parts) < 2:
                continue
            left = self._normalize_path(parts[0].strip())
            right = self._normalize_path(parts[1].strip())
            if direction == "to_src":
                if left and path_norm.startswith(left):
                    return path_norm.replace(left, right, 1)
            else:
                if right and path_norm.startswith(right):
                    return path_norm.replace(right, left, 1)
        return path_norm

    def handle_file_delete(self, file_path: str):
        if not self.get_state():
            return
        file_path = self._normalize_path(file_path)
        if not self._is_in_monitor_dirs(file_path):
            return
        if not self._is_media_file(file_path):
            return
        if self._delay_delete > 0:
            for _ in range(3):
                time.sleep(self._delay_delete / 3)
                if os.path.exists(file_path):
                    logger.info(f"[硬链接反向删除] 文件已恢复(重命名/移动)，跳过删除: {file_path}")
                    return
        if os.path.exists(file_path):
            return
        self._process_deleted_file(file_path)

    def _process_deleted_file(self, file_path: str):
        logger.info(f"[硬链接反向删除] 处理文件删除: {file_path}")
        self._log_action(f"处理文件删除: {os.path.basename(file_path)}")

        mp_path = self._map_path(file_path, direction="to_mp")
        mapped_src = self._map_path(file_path, direction="to_src")
        logger.debug(f"[硬链接反向删除] 路径映射: 原始={file_path}, MP路径={mp_path}, 源路径={mapped_src}")

        torrent_hash = None
        downloader_name = None
        src_path = None

        try:
            transfer_list = self._transferhis.get_by(dest=mp_path)
            logger.debug(f"[硬链接反向删除] transferhis.get_by(dest={mp_path}) 返回 {len(transfer_list) if transfer_list else 0} 条记录")
            if not transfer_list and mp_path != file_path:
                transfer_list = self._transferhis.get_by(dest=file_path)
                logger.debug(f"[硬链接反向删除] transferhis.get_by(dest={file_path}) 返回 {len(transfer_list) if transfer_list else 0} 条记录")
            if not transfer_list:
                parent = self._parent_dir(mp_path)
                transfer_list = self._transferhis.get_by(dest=parent)
                logger.debug(f"[硬链接反向删除] transferhis.get_by(dest={parent}) 返回 {len(transfer_list) if transfer_list else 0} 条记录")
                if not transfer_list and parent != self._parent_dir(file_path):
                    parent2 = self._parent_dir(file_path)
                    transfer_list = self._transferhis.get_by(dest=parent2)
                    logger.debug(f"[硬链接反向删除] transferhis.get_by(dest={parent2}) 返回 {len(transfer_list) if transfer_list else 0} 条记录")
            if transfer_list:
                for th in transfer_list:
                    if th and hasattr(th, 'download_hash') and th.download_hash:
                        torrent_hash = str(th.download_hash)
                        src_path = getattr(th, 'src', '') or ''
                        logger.info(f"[硬链接反向删除] 从转移历史找到hash: {torrent_hash}, src: {src_path}")
                        break
        except Exception as e:
            logger.error(f"[硬链接反向删除] 查询转移历史失败: {str(e)}", exc_info=True)

        if not torrent_hash:
            try:
                torrent_hash = self._downloadhis.get_hash_by_fullpath(mapped_src)
                logger.debug(f"[硬链接反向删除] downloadhis.get_hash_by_fullpath({mapped_src}) = {torrent_hash}")
                if not torrent_hash and mapped_src != file_path:
                    torrent_hash = self._downloadhis.get_hash_by_fullpath(file_path)
                    logger.debug(f"[硬链接反向删除] downloadhis.get_hash_by_fullpath({file_path}) = {torrent_hash}")
                if torrent_hash:
                    try:
                        dl = self._downloadhis.get_by_hash(torrent_hash)
                        if dl:
                            downloader_name = getattr(dl, 'downloader', '') or ''
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"[硬链接反向删除] 从下载历史查询hash失败: {str(e)}")

        if torrent_hash:
            logger.info(f"[硬链接反向删除] 找到关联种子hash: {torrent_hash}")
            self._handle_torrent(torrent_hash, src_path or self._map_path(file_path, direction="to_src"), downloader_name)
        else:
            self._handle_torrent_by_scan(file_path, self._map_path(file_path, direction="to_src"))

    def _handle_torrent(self, torrent_hash: str, src_path: str, downloader_name: str = None):
        try:
            try:
                self._downloadhis.delete_file_by_fullpath(fullpath=src_path)
            except Exception:
                pass
            if not downloader_name:
                try:
                    dl = self._downloadhis.get_by_hash(torrent_hash)
                    if dl:
                        downloader_name = getattr(dl, 'downloader', '') or ''
                except Exception:
                    pass
            download_files = self._downloadhis.get_files_by_hash(download_hash=torrent_hash)
            if not download_files:
                logger.warning(f"[硬链接反向删除] 未找到种子 {torrent_hash} 的文件记录，直接删除种子")
                self.chain.remove_torrents(hashs=torrent_hash, downloader=downloader_name)
                self._log_action(f"删除种子(无文件记录): {torrent_hash[:16]}...")
                return
            no_del_cnt = 0
            for df in download_files:
                if df and hasattr(df, 'state') and df.state and int(df.state) == 1:
                    no_del_cnt += 1
            if no_del_cnt > 0:
                logger.info(f"[硬链接反向删除] 种子 {torrent_hash} 还有 {no_del_cnt} 个文件未删除，暂停种子")
                self.chain.stop_torrents(hashs=torrent_hash, downloader=downloader_name)
                self._log_action(f"暂停种子(仍有{no_del_cnt}个文件): {torrent_hash[:16]}...")
            else:
                logger.info(f"[硬链接反向删除] 种子 {torrent_hash} 所有文件记录已删除，删除种子")
                self.chain.remove_torrents(hashs=torrent_hash, downloader=downloader_name)
                self._log_action(f"删除种子(所有文件已删): {torrent_hash[:16]}...")
        except Exception as e:
            logger.error(f"[硬链接反向删除] 处理种子 {torrent_hash} 失败: {str(e)}", exc_info=True)

    def _get_torrent_save_path(self, torrent) -> str:
        save_path = getattr(torrent, 'save_path', '') or ''
        if not save_path:
            save_path = getattr(torrent, 'download_dir', '') or ''
        return str(save_path)

    def _get_torrent_file_paths(self, torrent) -> List[str]:
        file_paths = []
        save_path = self._normalize_path(self._get_torrent_save_path(torrent))
        torrent_files = getattr(torrent, 'files', []) or []
        for tf in torrent_files:
            if hasattr(tf, 'name'):
                rel_path = str(tf.name)
            elif hasattr(tf, 'path'):
                rel_path = str(tf.path)
            else:
                rel_path = str(tf)
            rel_norm = self._normalize_path(rel_path)
            if not self._is_media_file(rel_norm):
                continue
            if save_path:
                full_path = f"{save_path}/{rel_norm}" if rel_norm else save_path
            else:
                full_path = rel_norm
            file_paths.append(full_path)
        if not file_paths and save_path:
            torrent_name = getattr(torrent, 'name', '')
            if torrent_name and self._is_media_file(str(torrent_name)):
                file_paths.append(f"{save_path}/{self._normalize_path(str(torrent_name))}")
        return file_paths

    def _get_all_downloaders(self):
        downloader_services = []
        try:
            services = self._downloader_helper.get_services()
            if services and isinstance(services, dict):
                for service_name, service_info in services.items():
                    if service_info and hasattr(service_info, 'instance') and service_info.instance:
                        downloader_services.append((str(service_name), service_info.instance))
            elif services:
                for service in services:
                    instance = getattr(service, 'instance', None) or service
                    name = getattr(service, 'name', 'default')
                    downloader_services.append((str(name), instance))
        except Exception as e:
            logger.debug(f"[硬链接反向删除] 获取下载器列表失败: {str(e)}")
        return downloader_services

    def _handle_torrent_by_scan(self, file_path: str, mapped_path: str):
        downloader_services = self._get_all_downloaders()
        if not downloader_services:
            logger.warning("[硬链接反向删除] 未找到启用的下载器")
            self._log_action("未找到启用的下载器")
            return

        deleted_hashes = set()
        file_norm = self._normalize_path(file_path)
        parent_norm = self._parent_dir(file_norm)
        dir_name = os.path.basename(parent_norm)
        mapped_parent = self._parent_dir(self._normalize_path(mapped_path))

        for service_name, downloader in downloader_services:
            try:
                torrents = downloader.get_torrents()
                for torrent in torrents:
                    torrent_hash = getattr(torrent, 'hash', '') or getattr(torrent, 'hashString', '')
                    if not torrent_hash or torrent_hash in deleted_hashes:
                        continue
                    torrent_name = getattr(torrent, 'name', str(torrent_hash))
                    file_paths = self._get_torrent_file_paths(torrent)
                    if not file_paths:
                        continue
                    match_found = False
                    for tf_path in file_paths:
                        tf_norm = self._normalize_path(tf_path)
                        linked_path = self._map_path(tf_norm, direction="to_mp")
                        if tf_norm == file_norm or tf_norm == self._normalize_path(mapped_path):
                            match_found = True
                            break
                        if linked_path and linked_path == file_norm:
                            match_found = True
                            break
                    if not match_found:
                        save_path = self._normalize_path(self._get_torrent_save_path(torrent))
                        torrent_name_norm = self._normalize_path(str(torrent_name))
                        if save_path:
                            if self._path_starts_with(save_path, mapped_parent):
                                match_found = True
                            elif self._parent_dir(save_path) == mapped_parent and dir_name and dir_name in torrent_name_norm:
                                match_found = True
                    if not match_found:
                        continue
                    deleted_hashes.add(torrent_hash)
                    existing_count = 0
                    for tf_path in file_paths:
                        tf_norm = self._normalize_path(tf_path)
                        if tf_norm == file_norm:
                            continue
                        linked_path = self._map_path(tf_norm, direction="to_mp")
                        if linked_path and os.path.exists(linked_path):
                            existing_count += 1
                        elif os.path.exists(tf_norm):
                            existing_count += 1
                    try:
                        if existing_count > 0:
                            logger.info(f"[硬链接反向删除] 扫描匹配到种子({service_name})，还有{existing_count}个文件存在，暂停: {torrent_name}")
                            self.chain.stop_torrents(hashs=torrent_hash, downloader=service_name)
                            self._log_action(f"暂停种子({service_name}): {str(torrent_name)[:50]}")
                        else:
                            logger.info(f"[硬链接反向删除] 扫描匹配到种子({service_name})，所有文件已删除，删除: {torrent_name}")
                            self.chain.remove_torrents(hashs=torrent_hash, downloader=service_name)
                            self._log_action(f"删除种子({service_name}): {str(torrent_name)[:50]}")
                    except Exception as e:
                        logger.error(f"[硬链接反向删除] 操作种子失败: {torrent_name}, 错误: {str(e)}")
            except Exception as e:
                logger.error(f"[硬链接反向删除] 遍历下载器失败({service_name}): {str(e)}")

        if not deleted_hashes:
            logger.info(f"[硬链接反向删除] 未找到关联种子(目录: {dir_name})")
            self._log_action(f"未找到关联种子: {dir_name}")

    @staticmethod
    def _path_starts_with(path: str, prefix: str) -> bool:
        if not path or not prefix:
            return False
        p = path.rstrip('/')
        pre = prefix.rstrip('/')
        return p == pre or p.startswith(pre + "/")

    def scan_orphan_torrents(self):
        if not self.get_state():
            return
        monitor_dirs = self._parse_monitor_dirs()
        if not monitor_dirs:
            return

        logger.info("[硬链接反向删除] 开始扫描孤儿种子")
        self._log_action("开始扫描孤儿种子")
        self._scan_orphans_by_history(monitor_dirs)

    def _scan_orphans_by_history(self, monitor_dirs: List[str]):
        deleted_count = 0
        paused_count = 0
        try:
            all_downloads = self._downloadhis.list()
            if not all_downloads:
                logger.info("[硬链接反向删除] 无下载历史记录")
                return
            hash_groups = {}
            for dl in all_downloads:
                if not dl or not hasattr(dl, 'download_hash') or not dl.download_hash:
                    continue
                h = str(dl.download_hash)
                if h not in hash_groups:
                    hash_groups[h] = []
                hash_groups[h].append(dl)
            for torrent_hash, download_files in hash_groups.items():
                try:
                    downloader_name = ''
                    for df in download_files:
                        if hasattr(df, 'downloader') and df.downloader:
                            downloader_name = str(df.downloader)
                            break
                    monitored_total = 0
                    existing_cnt = 0
                    for df in download_files:
                        file_path_str = None
                        if hasattr(df, 'path'):
                            file_path_str = df.path
                        elif hasattr(df, 'fullpath'):
                            file_path_str = df.fullpath
                        if not file_path_str:
                            continue
                        src_norm = self._normalize_path(str(file_path_str))
                        if not self._is_media_file(src_norm):
                            continue
                        in_monitor = False
                        for md in monitor_dirs:
                            md_norm = self._normalize_path(md)
                            mp_path = self._map_path(src_norm, direction="to_mp")
                            if self._path_starts_with(mp_path, md_norm):
                                in_monitor = True
                                break
                            if self._path_starts_with(src_norm, md_norm):
                                in_monitor = True
                                break
                        if not in_monitor:
                            continue
                        monitored_total += 1
                        if hasattr(df, 'state') and df.state and int(df.state) == 1:
                            existing_cnt += 1
                    if monitored_total == 0:
                        continue
                    try:
                        if existing_cnt == 0:
                            self.chain.remove_torrents(hashs=torrent_hash, downloader=downloader_name)
                            deleted_count += 1
                            logger.info(f"[硬链接反向删除] 删除孤儿种子: {torrent_hash[:16]}...")
                            self._log_action(f"删除孤儿种子: {torrent_hash[:16]}...")
                        elif existing_cnt < monitored_total:
                            self.chain.stop_torrents(hashs=torrent_hash, downloader=downloader_name)
                            paused_count += 1
                            logger.info(f"[硬链接反向删除] 部分文件已删除({existing_cnt}/{monitored_total})，暂停种子: {torrent_hash[:16]}...")
                            self._log_action(f"暂停种子(部分缺失{existing_cnt}/{monitored_total}): {torrent_hash[:16]}...")
                    except Exception as e:
                        logger.error(f"[硬链接反向删除] 处理孤儿种子失败 {torrent_hash}: {str(e)}")
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"[硬链接反向删除] 孤儿扫描(历史记录模式)失败: {str(e)}")

        logger.info(f"[硬链接反向删除] 孤儿种子扫描完成，删除 {deleted_count} 个，暂停 {paused_count} 个")
        self._log_action(f"孤儿扫描完成，删除{deleted_count}个，暂停{paused_count}个")
