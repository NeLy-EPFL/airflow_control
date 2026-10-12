from airflow_control.sim import sim_mfc


def fake_mfc(name="air", address=1, cap=None, **kw):
    return sim_mfc(name, address, cap, **kw)
