import product_monitor as pm


def make_scraper():
    # BaseScraper.__init__ only stores the session, never calls it
    # synchronously - passing None keeps these tests network-free.
    return pm.BaseScraper(None)


class TestParsePrice:
    def test_plain_number(self):
        assert make_scraper().parse_price("15000") == 15000

    def test_thousands_separator_and_currency(self):
        assert make_scraper().parse_price("15 000 Ft") == 15000

    def test_empty_string(self):
        assert make_scraper().parse_price("") == 0

    def test_no_digits(self):
        assert make_scraper().parse_price("Ingyenes") == 0


class TestNormalizeText:
    def test_removes_hungarian_diacritics(self):
        assert make_scraper().normalize_text("Árvíztűrő") == "arvizturo"

    def test_lowercases(self):
        assert make_scraper().normalize_text("XBOX Series S") == "xbox series s"

    def test_cache_is_consistent_on_repeat(self):
        scraper = make_scraper()
        first = scraper.normalize_text("Orwell George")
        second = scraper.normalize_text("Orwell George")
        assert first == second == "orwell george"


class TestMatchesSearchTerms:
    def test_empty_terms_always_match(self):
        assert make_scraper().matches_search_terms("Bármi cím", []) is True

    def test_all_terms_must_be_present(self):
        scraper = make_scraper()
        assert (
            scraper.matches_search_terms("Xbox Series S 512GB", ["xbox", "series", "512gb"]) is True
        )
        assert scraper.matches_search_terms("Xbox Series X", ["xbox", "series", "512gb"]) is False

    def test_matching_is_substring_not_whole_word(self):
        # matches_search_terms checks `term in title`, not word-boundaries -
        # a single-letter term like "s" matches almost anything containing
        # that letter, e.g. "series".
        scraper = make_scraper()
        assert scraper.matches_search_terms("Xbox Series X 512GB", ["s"]) is True

    def test_combined_phrase_term_gets_word_boundary_precision_for_free(self):
        # The real config used to search Xbox Series S with two separate
        # AND-ed terms, ["series", "s"] - the lone "s" matched "Series X"
        # too (see the test above). Rewriting it as a single combined
        # phrase term, ["series s"], sidesteps the substring-matching
        # looseness: "series s" as one substring only occurs when "series"
        # is directly followed by " s", so a "Series X" listing no longer
        # slips through - no code change needed, just a config-level fix.
        scraper = make_scraper()
        assert scraper.matches_search_terms("Xbox Series S 512GB", ["series s"]) is True
        assert scraper.matches_search_terms("Xbox Series X 512GB", ["series s"]) is False

    def test_diacritic_and_case_insensitive(self):
        scraper = make_scraper()
        assert (
            scraper.matches_search_terms("Árvíztűrő tükörfúrógép", ["arvizturo", "tukorfurogep"])
            is True
        )


class TestGetSearchId:
    def test_deterministic(self):
        cfg1 = pm.SearchConfig(site="Vatera", search_terms=["orwell"], max_price=10000)
        cfg2 = pm.SearchConfig(site="Vatera", search_terms=["orwell"], max_price=10000)
        assert cfg1.get_search_id() == cfg2.get_search_id()

    def test_different_terms_different_id(self):
        cfg1 = pm.SearchConfig(site="Vatera", search_terms=["orwell"], max_price=10000)
        cfg2 = pm.SearchConfig(site="Vatera", search_terms=["beta"], max_price=10000)
        assert cfg1.get_search_id() != cfg2.get_search_id()

    def test_moly_uses_url_not_terms(self):
        cfg = pm.SearchConfig(
            site="Moly",
            search_terms=[],
            max_price=15000,
            url="https://moly.hu/konyvek/example/elado-peldanyok",
        )
        # should not raise even though search_terms is empty - Moly keys off .url
        assert len(cfg.get_search_id()) == 8


class TestMonitorConfigFromDict:
    def test_email_comes_from_module_level_env_config_not_json(self):
        data = {
            "searches": [
                {"site": "Vatera", "search_terms": ["x"], "max_price": 1000},
            ],
            "check_interval": 60,
            "cleanup_days": 10,
        }
        config = pm.MonitorConfig.from_dict(data)
        assert config.email is pm.EMAIL_CONFIG
        assert set(config.email.keys()) == {
            "smtp_server",
            "smtp_port",
            "sender_email",
            "sender_password",
            "recipient_email",
        }

    def test_defaults(self):
        data = {"searches": [], "check_interval": 1, "cleanup_days": 1}
        config = pm.MonitorConfig.from_dict(data)
        assert config.max_concurrent_requests == 5
        assert config.request_timeout == 60
