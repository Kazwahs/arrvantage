"""
Tests for pure logic functions - status categorization, name
deduplication, size formatting, genre extraction. None of these touch
the network or the filesystem, so they're the fastest and most
reliable tests in the suite.
"""


class TestGetStatusCategory:
    def test_movie_downloaded(self, app_module):
        record = {"hasFile": True}
        assert app_module.get_status_category("movie", record) == "Downloaded"

    def test_movie_missing(self, app_module):
        record = {"hasFile": False}
        assert app_module.get_status_category("movie", record) == "Missing"

    def test_movie_no_hasfile_field(self, app_module):
        # A record missing the field entirely should be treated the
        # same as hasFile: False, not raise a KeyError.
        record = {}
        assert app_module.get_status_category("movie", record) == "Missing"

    def test_series_complete(self, app_module):
        record = {"statistics": {"episodeFileCount": 10, "episodeCount": 10}}
        assert app_module.get_status_category("series", record) == "Complete"

    def test_series_partial(self, app_module):
        record = {"statistics": {"episodeFileCount": 4, "episodeCount": 10}}
        assert app_module.get_status_category("series", record) == "Partial"

    def test_series_missing(self, app_module):
        record = {"statistics": {"episodeFileCount": 0, "episodeCount": 10}}
        assert app_module.get_status_category("series", record) == "Missing"

    def test_series_unknown_when_total_zero(self, app_module):
        # A show with zero total episodes (e.g. not yet aired) has no
        # meaningful complete/partial/missing distinction to make.
        record = {"statistics": {"episodeFileCount": 0, "episodeCount": 0}}
        assert app_module.get_status_category("series", record) == "Unknown"

    def test_artist_complete(self, app_module):
        record = {"statistics": {"trackFileCount": 20, "trackCount": 20}}
        assert app_module.get_status_category("artist", record) == "Complete"

    def test_author_partial(self, app_module):
        record = {"statistics": {"bookFileCount": 2, "bookCount": 5}}
        assert app_module.get_status_category("author", record) == "Partial"

    def test_missing_statistics_dict_entirely(self, app_module):
        # Some records may genuinely lack a "statistics" key at all -
        # should degrade to Unknown, not raise.
        record = {}
        assert app_module.get_status_category("series", record) == "Unknown"


class TestDedupeNames:
    def test_no_duplicates_unchanged(self, app_module):
        entries = [{"name": "Radarr"}, {"name": "Sonarr"}]
        result = app_module.dedupe_names(entries)
        assert [e["name"] for e in result] == ["Radarr", "Sonarr"]

    def test_duplicate_gets_suffixed(self, app_module):
        entries = [{"name": "Radarr"}, {"name": "Radarr"}]
        result = app_module.dedupe_names(entries)
        names = [e["name"] for e in result]
        assert names == ["Radarr", "Radarr (2)"]

    def test_triple_duplicate(self, app_module):
        entries = [{"name": "X"}, {"name": "X"}, {"name": "X"}]
        result = app_module.dedupe_names(entries)
        names = [e["name"] for e in result]
        assert names == ["X", "X (2)", "X (3)"]

    def test_empty_list(self, app_module):
        assert app_module.dedupe_names([]) == []


class TestFormatSizeGb:
    def test_zero_bytes(self, app_module):
        assert app_module.format_size_gb(0) == "0.0 GB"

    def test_none_treated_as_zero(self, app_module):
        # fetch_home_stats sums size_bytes across items where some
        # entries may have None rather than 0 - this must not raise.
        assert app_module.format_size_gb(None) == "0.0 GB"

    def test_under_1tb_shows_gb(self, app_module):
        five_gb_in_bytes = 5 * (1024 ** 3)
        assert app_module.format_size_gb(five_gb_in_bytes) == "5.0 GB"

    def test_over_1tb_shows_tb(self, app_module):
        two_tb_in_bytes = 2 * (1024 ** 4)
        assert app_module.format_size_gb(two_tb_in_bytes) == "2.0 TB"

    def test_boundary_just_under_1tb(self, app_module):
        just_under = 1023 * (1024 ** 3)
        result = app_module.format_size_gb(just_under)
        assert result.endswith("GB")


class TestGetGenre:
    def test_returns_joined_genres(self, app_module):
        record = {"genres": ["Action", "Sci-Fi"]}
        assert app_module.get_genre(record) == "Action, Sci-Fi"

    def test_empty_list_returns_unknown(self, app_module):
        record = {"genres": []}
        assert app_module.get_genre(record) == "Unknown"

    def test_missing_field_returns_unknown(self, app_module):
        record = {}
        assert app_module.get_genre(record) == "Unknown"

    def test_single_genre(self, app_module):
        record = {"genres": ["Drama"]}
        assert app_module.get_genre(record) == "Drama"
