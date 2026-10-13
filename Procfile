web: python manage.py migrate --noinput && gunicorn labfund.wsgi --bind 0.0.0.0:$PORT --workers 2 --timeout 120
