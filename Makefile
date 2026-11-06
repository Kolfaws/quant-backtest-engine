# =============================================================================
# quant-backtest-engine — 常用命令
#
# 默认使用 uv；若已激活自己的 Python 环境，可用 make RUN= test。
# =============================================================================

.DEFAULT_GOAL := help

UV ?= uv
RUN ?= $(UV) run
PYTHON ?= $(RUN) python
PYTEST ?= $(UV) run --with pytest python -m pytest

DB ?= $(BACKTEST_DB_PATH)
PROFILE ?=
BENCHMARK ?= BI_EQW_CORE
RUN_ID ?= demo_eqw_core

.PHONY: help install init init-test sync-test check test demo nav promote-test clean

help:
	@echo "backtest_engine"
	@echo ""
	@echo "  install   uv sync"
	@echo "  init      初始化 backtest.duckdb（单事务 + 校验 + 版本）"
	@echo "  init-test 初始化 backtest_test.duckdb"
	@echo "  sync-test 正式基准 -> 测试库"
	@echo "  promote-test 测试 run -> 正式库（默认 dry-run）"
	@echo "  check     校验表结构与版本"
	@echo "  demo      用 market.duckdb 的 5 只核心票跑 demo 净值"
	@echo "  nav       从 CSV 写净值（CSV=$(CSV) RUN_ID=$(RUN_ID)）"
	@echo "  test      pytest"
	@echo "  clean     清理缓存"
	@echo ""
	@echo "当前 DB=$(DB) PROFILE=$(PROFILE)"

install:
	$(UV) sync

init:
	$(PYTHON) scripts/init_backtest_db.py init $(if $(DB),--db "$(DB)",)

init-test:
	$(PYTHON) scripts/init_test_db.py

sync-test:
	$(PYTHON) scripts/sync_test_backtest.py --apply

check:
	$(PYTHON) scripts/init_backtest_db.py check $(if $(DB),--db "$(DB)",)

test:
	$(PYTEST) tests -q

demo:
	$(PYTHON) scripts/demo_run.py --run-id $(RUN_ID) --benchmark-id $(BENCHMARK) $(if $(PROFILE),--profile $(PROFILE),)

nav:
	@test -n "$(CSV)" || (echo "用法: make nav CSV=path/to/returns.csv RUN_ID=my_run" && exit 2)
	$(PYTHON) scripts/run_nav.py --run-id $(RUN_ID) --strategy-id $(RUN_ID) --returns $(CSV) $(if $(BENCHMARK),--benchmark-id $(BENCHMARK),)

promote-test:
	$(PYTHON) scripts/promote_test_run.py --run-id $(RUN_ID)

clean:
	$(PYTHON) -c "import pathlib, shutil; [shutil.rmtree(p, ignore_errors=True) for p in map(pathlib.Path, ('.pytest_cache','.ruff_cache','.mypy_cache','htmlcov','.coverage')) if p.exists()]"
	$(PYTHON) -c "import pathlib, shutil; [shutil.rmtree(p, ignore_errors=True) for r in (pathlib.Path('src'), pathlib.Path('scripts'), pathlib.Path('tests')) if r.exists() for p in r.rglob('__pycache__')]"
