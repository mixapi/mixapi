from mixapi.errors import configuration_unavailable, control_plane_unavailable


def test_control_plane_dependency_errors_are_normalized_503_errors() -> None:
    control_plane = control_plane_unavailable()
    configuration = configuration_unavailable()

    assert (
        control_plane.type,
        control_plane.code,
        control_plane.status_code,
    ) == ("control_plane_unavailable", "control_plane_unavailable", 503)
    assert (
        configuration.type,
        configuration.code,
        configuration.status_code,
    ) == ("configuration_unavailable", "configuration_unavailable", 503)
