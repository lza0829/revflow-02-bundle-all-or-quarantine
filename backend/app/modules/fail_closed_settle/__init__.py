"""归并整单失败闭与回滚。

实现位置：
- 捆扎「全成或全进未平」的单事务失败闭见
  ``app.services.settle.run_settle`` / ``run_bundle_settle``
  （成员状态 + 余额 + 隔离 + 捆状态同一事务，异常整体 rollback）；
- 捆扎身份与整捆预演见 :mod:`app.modules.multi_leg_net`。

回滚保证由 SQLite 连接事务（``with connect() as conn`` + 显式 ``commit``）提供，
故障注入回滚用例见 ``backend/tests/test_multi_leg_net.py``。
"""
