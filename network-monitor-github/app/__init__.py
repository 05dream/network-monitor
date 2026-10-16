import os

from flask import Flask

from .config import Config


def create_app():
    app = Flask(__name__,
                template_folder=os.path.join('web', 'templates'),
                static_folder=os.path.join('web', 'static'))
    app.config.from_object(Config)

    from . import db
    db.init_app(app)

    from . import auth
    app.register_blueprint(auth.bp)

    from . import pages
    app.register_blueprint(pages.bp)

    from . import api
    app.register_blueprint(api.bp)

    return app
