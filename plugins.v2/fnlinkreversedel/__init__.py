import os
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Tuple, Optional

from apscheduler.triggers.interval import IntervalTrigger

from app import schemas
from app.chain.storage import StorageChain
from app.core.config import settings
from app.core.event import eventmanager, Event
from app.db.downloadhistory_oper import DownloadHistoryOper
from app.db.transferhistory_oper import TransferHistoryOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType


class FnLinkReverseDel(_PluginBase):
    plugin_name = "硬链接反向删除"
    plugin_desc = "监控硬链接目录，文件删除时同步删除关联种子"
    plugin_icon = "mediasyncdel.png"
    plugin_version = "5.3"
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
    _force_polling = False
    _watch_thread = None
    _watch_running = False
    _scheduler = None
    _transferhis = None
    _downloadhis = None
    _storagechain = None

    @staticmethod
    def _safe_int(value, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def init_plugin(self, config: dict = None):
        self.stop_service()
        # 可变状态属性在实例上初始化，避免类级别共享
        self._processing_paths = set()
        self._processing_lock = threading.Lock()
        self._recent_processed = {}
        self._transferhis = TransferHistoryOper()
        self._downloadhis = DownloadHistoryOper()
        self._storagechain = StorageChain()

        if config:
            self._enabled = bool(config.get("enabled"))
            self._monitor_dirs = config.get("monitor_dirs") or ""
            self._path_mappings = config.get("path_mappings") or ""
            self._exclude_keywords = config.get("exclude_keywords") or ""
            self._delay_delete = self._safe_int(config.get("delay_delete"), 5)
            self._orphan_scan_interval = self._safe_int(config.get("orphan_scan_interval"), 3600)
            self._force_polling = bool(config.get("force_polling"))
        # 缓存监控目录列表，避免每次事件都重新解析（性能优化）
        self._monitor_dirs_cache = self._parse_monitor_dirs() if self._monitor_dirs else []
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
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {'model': 'force_polling', 'label': '强制轮询(SMB/NFS)'}}
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
            "force_polling": False,
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
        # 使用 init_plugin 时缓存的监控目录列表，避免每次事件都重新解析
        monitor_dirs = self._monitor_dirs_cache or self._parse_monitor_dirs()
        file_path_norm = self._normalize_path(os.path.normpath(file_path))
        for monitor_dir in monitor_dirs:
            monitor_dir_norm = self._normalize_path(os.path.normpath(monitor_dir))
            if file_path_norm == monitor_dir_norm:
                return True
            if file_path_norm.startswith(monitor_dir_norm + "/"):
                return True
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

            for changes in watch(*normalized_dirs, watch_filter=_DeleteFilter(self), force_polling=self._force_polling):
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
        # 双重去重：_processing_paths(处理中) + _recent_processed(120秒窗口)
        # 解决watchfiles对同一次删除触发多次事件导致的重复处理
        with self._processing_lock:
            # 清理过期的 _recent_processed 记录
            now = time.time()
            expired = [k for k, v in self._recent_processed.items() if now - v > 120]
            for k in expired:
                del self._recent_processed[k]
            # 检查1：120秒内已处理过，直接跳过
            if file_path in self._recent_processed:
                logger.debug(f"[硬链接反向删除] 重复事件(120秒窗口)，跳过: {file_path}")
                return
            # 检查2：正在处理中，直接跳过
            if file_path in self._processing_paths:
                logger.debug(f"[硬链接反向删除] 重复事件(处理中)，跳过: {file_path}")
                return
            self._processing_paths.add(file_path)
            self._recent_processed[file_path] = now
        try:
            self.handle_file_delete(file_path)
        finally:
            with self._processing_lock:
                self._processing_paths.discard(file_path)

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
                # 监控路径 → 源文件路径：left=监控目录, right=下载源目录
                if left and path_norm.startswith(left):
                    return path_norm.replace(left, right, 1)
            elif direction == "to_mp":
                # 源文件路径 → 监控路径：right=下载源目录, left=监控目录
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
        """处理硬链接文件删除事件（方案B：复用后端逻辑）

        流程：
        1. 通过 dest 路径查转移记录（含 src_fileitem/download_hash/downloader）
        2. 调用 StorageChain.delete_media_file 删源文件（与后端 API 一致，自动删空目录）
        3. 调用 DownloadFiles.delete_by_fullpath 删下载文件记录（state=0）
        4. 调用 TransferHistoryOper.delete 删转移记录
        5. 检查种子所有文件是否都已删除，若是才调用 chain.remove_torrents 删做种任务
        """
        # 去重检查已在 _async_handle_delete 入口完成（_recent_processed + _processing_paths 双重保险）
        logger.info(f"[硬链接反向删除] 处理文件删除: {file_path}")
        self._log_action(f"处理文件删除: {os.path.basename(file_path)}")

        # 步骤1：查转移记录（dest = 硬链接路径）
        histories = self._find_transfer_history(file_path)
        if not histories:
            logger.warning(f"[硬链接反向删除] 未找到转移记录，跳过: {file_path}")
            self._log_action(f"未找到转移记录: {os.path.basename(file_path)}")
            return

        # 步骤2-4：逐个处理转移记录（删源文件、删下载文件记录、删转移记录）
        processed_hashes = set()
        for history in histories:
            try:
                download_hash = self._delete_history_and_related(history)
                if download_hash:
                    processed_hashes.add(download_hash)
            except Exception as e:
                logger.error(f"[硬链接反向删除] 处理转移记录失败(id={getattr(history, 'id', '?')}): {str(e)}", exc_info=True)

        # 步骤5：检查种子所有文件是否都已删除，若是才删做种任务（避免误删整季）
        for download_hash in processed_hashes:
            try:
                self._remove_torrent_if_all_deleted(download_hash)
            except Exception as e:
                logger.error(f"[硬链接反向删除] 删除做种任务失败(hash={download_hash}): {str(e)}")

    def _find_transfer_history(self, dest_path: str) -> List:
        """通过 dest 路径查找转移记录，多级查询策略

        查询顺序：
        1. dest 精确匹配（get_by_dest 返回单条，再用 list_by_hash 扩展同 hash 的所有记录）
        2. 路径映射推断 src 后用 get_by_src 反查（同样用 list_by_hash 扩展）

        注意：移除了"父目录查询"策略，因为父目录的转移记录 src 可能是目录而非文件，
              会导致 StorageChain 误删整个目录（参考 P0 问题3）
        """
        # 策略1：dest 精确匹配
        try:
            history = self._transferhis.get_by_dest(dest_path)
            if history:
                logger.info(f"[硬链接反向删除] 通过dest精确路径找到转移记录: id={getattr(history, 'id', '?')}, src={getattr(history, 'src', '')}, download_hash={getattr(history, 'download_hash', None)}")
                # 用 list_by_hash 扩展：同一 hash 可能有多条转移记录（如多文件种子每集一条）
                # 关键：list_by_hash 单独 try-except，失败不影响返回 [history]
                download_hash = getattr(history, 'download_hash', None)
                if download_hash:
                    try:
                        all_histories = self._transferhis.list_by_hash(str(download_hash))
                        if all_histories and len(all_histories) > 1:
                            logger.info(f"[硬链接反向删除] 同hash共{len(all_histories)}条转移记录，全部处理")
                            return all_histories
                    except Exception as e:
                        logger.warning(f"[硬链接反向删除] list_by_hash扩展查询失败(非致命，使用单条记录): {str(e)}")
                return [history]
        except Exception as e:
            logger.error(f"[硬链接反向删除] dest精确查询失败: {str(e)}", exc_info=True)

        # 策略2：路径映射推断 src，再用 get_by_src 反查
        mapped_src = self._map_path(dest_path, direction="to_src")
        if mapped_src != dest_path:
            try:
                history = self._transferhis.get_by_src(mapped_src)
                if history:
                    logger.info(f"[硬链接反向删除] 通过路径映射+get_by_src找到转移记录: id={getattr(history, 'id', '?')}")
                    download_hash = getattr(history, 'download_hash', None)
                    if download_hash:
                        try:
                            all_histories = self._transferhis.list_by_hash(str(download_hash))
                            if all_histories and len(all_histories) > 1:
                                logger.info(f"[硬链接反向删除] 同hash共{len(all_histories)}条转移记录，全部处理")
                                return all_histories
                        except Exception as e:
                            logger.warning(f"[硬链接反向删除] list_by_hash扩展查询失败(非致命，使用单条记录): {str(e)}")
                    return [history]
            except Exception as e:
                logger.error(f"[硬链接反向删除] 路径映射反查失败: {str(e)}", exc_info=True)

        logger.warning(f"[硬链接反向删除] 未找到转移记录: {dest_path}")
        return []

    def _delete_history_and_related(self, history) -> Optional[str]:
        """删除单条转移记录及其关联资源（与后端 DELETE /api/v1/history/transfer 行为一致）

        顺序（参考后端 history.py:221）：
        1. 删源文件（StorageChain.delete_media_file，自动处理空目录）
        2. 删下载文件记录（DownloadFiles.delete_by_fullpath，state=0）
        3. 删转移记录（TransferHistory.delete）

        注意：不再在此方法删除做种任务，由调用方统一判断"所有文件是否都已删除"后决定是否删除种子

        :return: 成功处理的 download_hash（用于后续判断是否删种子），失败返回 None
        """
        # 提取字段
        his_id = getattr(history, 'id', None)
        src_path = getattr(history, 'src', '') or ''
        download_hash = getattr(history, 'download_hash', None) or ''
        src_fileitem_dict = getattr(history, 'src_fileitem', None) or {}

        if download_hash:
            download_hash = str(download_hash)

        # 步骤1：删除源文件（使用 StorageChain，与后端一致）
        if src_path:
            src_fileitem = None
            # 优先用转移记录中的 src_fileitem（包含完整存储信息，与后端 API 一致）
            if isinstance(src_fileitem_dict, dict) and src_fileitem_dict:
                try:
                    src_fileitem = schemas.FileItem(**src_fileitem_dict)
                except Exception as e:
                    logger.debug(f"[硬链接反向删除] 构造src_fileitem失败: {str(e)}")
            # 兜底：从路径构造（默认local存储，不检查文件是否存在，与后端一致）
            if src_fileitem is None and src_path:
                src_fileitem = self._build_fileitem_from_path(src_path)
            if src_fileitem:
                try:
                    # delete_media_file 会自动：删文件 → 删空父目录（不删媒体库根目录）
                    state = self._storagechain.delete_media_file(src_fileitem)
                    if state:
                        logger.info(f"[硬链接反向删除] 已删除源文件: {src_path}")
                        self._log_action(f"删除源文件: {os.path.basename(src_path)}")
                    else:
                        logger.warning(f"[硬链接反向删除] 源文件删除失败(可能已不存在): {src_path}")
                except Exception as e:
                    logger.error(f"[硬链接反向删除] 删除源文件异常: {src_path}, {str(e)}")

        # 步骤2：删除下载文件记录（state=0，与后端一致）
        # 关键：后端 DownloadFiles.fullpath 字段使用 POSIX 风格（Path.as_posix()），
        # delete_by_fullpath 用 == 精确匹配，所以这里必须用 Path(src_path).as_posix() 转换
        if src_path:
            try:
                # 统一转换为 POSIX 风格，与后端 fullpath 字段存储格式一致
                src_posix = Path(src_path).as_posix()
                self._downloadhis.delete_file_by_fullpath(fullpath=src_posix)
                logger.info(f"[硬链接反向删除] 已标记下载文件记录删除: {src_posix}")
            except Exception as e:
                logger.error(f"[硬链接反向删除] 标记下载文件记录失败: {str(e)}")

        # 步骤3：删除转移记录
        if his_id is not None:
            try:
                self._transferhis.delete(his_id)
                logger.info(f"[硬链接反向删除] 已删除转移记录: {his_id}")
                self._log_action(f"删除转移记录: {his_id}")
            except Exception as e:
                logger.error(f"[硬链接反向删除] 删除转移记录失败: {str(e)}")

        # download_hash 为空时，用 src_path 反查 downloadhis 获取 hash
        # 场景：手动整理、非下载来源，转移记录可能没有 download_hash
        if not download_hash and src_path:
            try:
                src_posix = Path(src_path).as_posix()
                # get_hash_by_fullpath 内部按 fullpath == src_posix 查询
                hash_from_db = self._downloadhis.get_hash_by_fullpath(src_posix)
                if hash_from_db:
                    download_hash = str(hash_from_db)
                    logger.info(f"[硬链接反向删除] 通过src反查到hash: {download_hash}")
            except Exception as e:
                logger.debug(f"[硬链接反向删除] src反查hash失败: {str(e)}")

        return download_hash or None

    def _remove_torrent_if_all_deleted(self, download_hash: str):
        """检查种子所有文件是否都已删除（state=0），若是才删除做种任务

        避免误删整季：多文件种子中，只有当所有文件都被标记删除（state=0）时才删种子任务。
        否则保留种子，让其他文件继续做种。

        :param download_hash: 种子hash
        """
        if not download_hash:
            return
        try:
            # 查询该 hash 的所有下载文件记录（不传 state 参数，查全部）
            dl_files = self._downloadhis.get_files_by_hash(download_hash)
            if not dl_files:
                logger.info(f"[硬链接反向删除] hash无下载文件记录，跳过删种子: {download_hash[:16]}...")
                return
            # 检查是否所有文件都已标记删除（state != 1）
            has_active = False
            total_files = len(dl_files)
            active_files = 0
            for df in dl_files:
                # DownloadFiles.state: 0=已删除, 1=正常
                # 兼容 int/str/None 多种类型
                df_state_raw = getattr(df, 'state', None)
                try:
                    df_state = int(df_state_raw) if df_state_raw is not None else 1
                except (TypeError, ValueError):
                    df_state = 1
                if df_state == 1:
                    has_active = True
                    active_files += 1
                    df_fullpath = getattr(df, 'fullpath', '') or ''
                    logger.debug(f"[硬链接反向删除] 仍有活跃文件: state={df_state}, fullpath={df_fullpath}")
            if has_active:
                logger.info(f"[硬链接反向删除] 种子仍有{active_files}/{total_files}个活跃文件(state=1)，保留种子: {download_hash[:16]}...")
                return
            # 所有文件都已删除，安全删除种子任务
            # 查询 downloader 名称
            downloader_name = ''
            try:
                dl = self._downloadhis.get_by_hash(download_hash)
                if dl:
                    downloader_name = getattr(dl, 'downloader', '') or ''
            except Exception:
                pass
            try:
                if downloader_name:
                    logger.info(f"[硬链接反向删除] 删除做种任务: {download_hash}, downloader={downloader_name}, 共{total_files}个文件全部已删除")
                else:
                    logger.info(f"[硬链接反向删除] 删除做种任务: {download_hash}, 未指定下载器将使用系统默认, 共{total_files}个文件全部已删除")
                # delete_file=False 不删源文件（源文件已在前面步骤删除）
                self.chain.remove_torrents(hashs=download_hash, downloader=downloader_name)
                self._log_action(f"删除做种任务: {download_hash[:16]}...")
            except Exception as e:
                logger.error(f"[硬链接反向删除] 删除做种任务失败: {str(e)}")
        except Exception as e:
            logger.error(f"[硬链接反向删除] 检查种子文件状态失败: {str(e)}")

    def _cleanup_orphan_related_records(self, torrent_hash: str, monitor_dirs: List[str]):
        """删除孤儿种子关联的转移记录和源文件（孤儿扫描专用，方案B）

        复用 _find_transfer_history + _delete_history_and_related，与主流程一致
        增加监控目录校验：仅处理监控目录内的文件，避免误删非监控目录的转移记录
        """
        try:
            dl_files = self._downloadhis.get_files_by_hash(torrent_hash)
            if not dl_files:
                return
            for df in dl_files:
                # DownloadFiles 模型字段：downloader/download_hash/fullpath/savepath/filepath/torrentname/state
                src_path = getattr(df, 'fullpath', '') or ''
                if not src_path:
                    continue
                src_norm = self._normalize_path(str(src_path))
                if not self._is_media_file(src_norm):
                    continue
                # 校验：源文件路径映射后必须在监控目录内，避免误删非监控目录的转移记录
                mp_path = self._map_path(src_norm, direction="to_mp")
                in_monitor = False
                for md in monitor_dirs:
                    md_norm = self._normalize_path(md)
                    if self._path_starts_with(mp_path, md_norm) or self._path_starts_with(src_norm, md_norm):
                        in_monitor = True
                        break
                if not in_monitor:
                    logger.debug(f"[硬链接反向删除] 孤儿扫描跳过非监控目录文件: {src_norm}")
                    continue
                # 查询并删除转移记录（仅处理监控目录内的）
                histories = self._find_transfer_history(mp_path)
                for history in histories:
                    try:
                        self._delete_history_and_related(history)
                    except Exception as e:
                        logger.debug(f"[硬链接反向删除] 孤儿扫描删除转移记录失败: {str(e)}")
        except Exception as e:
            logger.debug(f"[硬链接反向删除] 孤儿扫描清理关联记录失败({torrent_hash}): {str(e)}")

    @staticmethod
    def _build_fileitem_from_path(path_str: str) -> Optional[schemas.FileItem]:
        """从路径字符串构造FileItem对象（默认local存储）

        :param path_str: 文件路径
        :return: FileItem对象或None
        """
        try:
            path = Path(path_str)
            # 不检查文件是否存在，与后端 API 一致
            # StorageChain.delete_media_file 内部会处理文件不存在的情况
            # 检查 exists() 会在网络存储场景下误判（如 SMB/NFS 未挂载时返回 False）
            # 判断是文件还是目录（无法判断时默认按文件处理）
            file_type = "file"
            try:
                file_type = "file" if path.is_file() else "dir"
            except Exception:
                pass
            extension = path.suffix[1:] if path.suffix and file_type == "file" else None
            return schemas.FileItem(
                storage="local",
                type=file_type,
                path=str(path),
                name=path.name,
                basename=path.stem,
                extension=extension,
            )
        except Exception as e:
            logger.debug(f"[硬链接反向删除] 构造FileItem失败({path_str}): {str(e)}")
            return None

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
        """流式扫描孤儿种子，避免一次性加载所有历史记录导致内存压力

        策略：分批加载 + 按hash分组 + 处理完即释放
        - 每批 1000 条，加载后按 hash 分组
        - 同一 hash 的文件可能跨页，所以用字典累积
        - 每处理完一个 hash 立即从字典移除，释放内存
        """
        deleted_count = 0
        try:
            BATCH_SIZE = 1000
            page = 1
            hash_groups = {}
            while True:
                batch = self._downloadhis.list_by_page(page=page, count=BATCH_SIZE)
                if not batch:
                    break
                # 当前批次按 hash 分组
                new_hashes_in_batch = set()
                for dl in batch:
                    if not dl or not hasattr(dl, 'download_hash') or not dl.download_hash:
                        continue
                    h = str(dl.download_hash)
                    if h not in hash_groups:
                        hash_groups[h] = []
                        new_hashes_in_batch.add(h)
                    hash_groups[h].append(dl)
                # 流式处理：处理完上一批次就已完整的 hash（本批次未再出现）
                # 简化策略：仅在最后一批处理完所有 hash
                if len(batch) < BATCH_SIZE:
                    break
                page += 1
            # 所有数据加载完毕后，逐个hash处理（处理完即释放）
            for torrent_hash in list(hash_groups.keys()):
                download_files = hash_groups.pop(torrent_hash, [])
                try:
                    result = self._process_orphan_hash(torrent_hash, download_files, monitor_dirs)
                    if result:
                        deleted_count += 1
                except Exception as e:
                    logger.error(f"[硬链接反向删除] 处理hash失败 {torrent_hash}: {str(e)}")
        except Exception as e:
            logger.error(f"[硬链接反向删除] 孤儿扫描(历史记录模式)失败: {str(e)}")

        logger.info(f"[硬链接反向删除] 孤儿种子扫描完成，删除 {deleted_count} 个")
        self._log_action(f"孤儿扫描完成，删除{deleted_count}个")

    def _process_orphan_hash(self, torrent_hash: str, download_files: list, monitor_dirs: List[str]) -> bool:
        """处理单个hash对应的孤儿种子判定和删除

        :param torrent_hash: 种子hash
        :param download_files: 该hash对应的下载文件列表
        :param monitor_dirs: 监控目录列表
        :return: 是否删除了种子
        """
        if not download_files or not monitor_dirs:
            return False
        try:
            downloader_name = ''
            for df in download_files:
                if hasattr(df, 'downloader') and df.downloader:
                    downloader_name = str(df.downloader)
                    break
            monitored_total = 0
            existing_cnt = 0
            for df in download_files:
                # DownloadFiles 模型字段：downloader/download_hash/fullpath/savepath/filepath/torrentname/state
                # 没有 path 字段，直接用 fullpath（完整路径）
                file_path_str = getattr(df, 'fullpath', '') or ''
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
                if hasattr(df, 'state') and df.state:
                    # 兼容 int/str 类型，避免 int() 抛 ValueError 导致整个 hash 处理中断
                    try:
                        df_state = int(df.state)
                    except (TypeError, ValueError):
                        df_state = 1
                    if df_state == 1:
                        existing_cnt += 1
            if monitored_total == 0:
                return False
            # 只有所有监控目录内的文件都不存在了（existing_cnt == 0）才是孤儿
            if existing_cnt > 0:
                return False
            try:
                # 删除孤儿种子（文件已全部被删除但种子还在做种）
                self.chain.remove_torrents(hashs=torrent_hash, downloader=downloader_name)
                logger.info(f"[硬链接反向删除] 删除孤儿种子: {torrent_hash[:16]}...")
                self._log_action(f"删除孤儿种子: {torrent_hash[:16]}...")
                # 同步删除该种子关联的转移记录和源文件（与主流程一致）
                self._cleanup_orphan_related_records(torrent_hash, monitor_dirs)
                return True
            except Exception as e:
                logger.error(f"[硬链接反向删除] 处理孤儿种子失败 {torrent_hash}: {str(e)}")
                return False
        except Exception:
            return False

