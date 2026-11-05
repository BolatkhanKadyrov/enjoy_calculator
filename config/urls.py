from django.urls import include, path
from django.contrib.auth import views as auth_views
from tseh import views
from tseh.auth import ThrottledLoginView

urlpatterns = [
    path("health/", views.health, name="health"),
    path("login/", ThrottledLoginView.as_view(template_name="tseh/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("tseh.urls")),
]
