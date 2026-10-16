from app import create_app

app = create_app()

if __name__ == '__main__':
    from app.collector import scheduler
    scheduler.start(app)
    # 后台采集线程要求关闭 reloader，否则会起两套调度
    app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False, threaded=True)
