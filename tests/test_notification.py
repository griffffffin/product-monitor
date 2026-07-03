import product_monitor as pm


def make_ad(site, ad_id, title, price):
    return pm.Advertisement(
        id=f"{site}_{ad_id}",
        title=title,
        price=price,
        url=f"https://example.com/{ad_id}",
        site=site,
        search_id="abc",
        first_seen="2026-01-01T00:00:00",
        last_seen="2026-01-01T00:00:00",
    )


class TestBuildNotification:
    def test_subject_counts_all_ads(self):
        ads = [make_ad("Vatera", "1", "Cím 1", 1000), make_ad("Moly", "2", "Cím 2", 2000)]
        subject, _ = pm.MultiMarketplaceMonitor.build_notification(ads)
        assert subject == "Új hirdetések: 2 db"

    def test_body_groups_by_site(self):
        ads = [
            make_ad("Vatera", "1", "Vatera Cím", 1000),
            make_ad("Moly", "2", "Moly Cím", 2000),
        ]
        _, body = pm.MultiMarketplaceMonitor.build_notification(ads)
        assert "Vatera" in body
        assert "Moly" in body
        assert "Vatera Cím" in body
        assert "Moly Cím" in body

    def test_price_formatted_with_space_thousands_separator(self):
        ads = [make_ad("Vatera", "1", "Cím", 15000)]
        _, body = pm.MultiMarketplaceMonitor.build_notification(ads)
        assert "15 000 Ft" in body

    def test_url_included(self):
        ads = [make_ad("Vatera", "1", "Cím", 1000)]
        _, body = pm.MultiMarketplaceMonitor.build_notification(ads)
        assert "https://example.com/1" in body

    def test_multiple_ads_same_site_are_numbered(self):
        ads = [make_ad("Vatera", "1", "Első", 1000), make_ad("Vatera", "2", "Második", 2000)]
        _, body = pm.MultiMarketplaceMonitor.build_notification(ads)
        assert "1. Első" in body
        assert "2. Második" in body

    def test_send_notification_noop_for_empty_list(self):
        monitor = object.__new__(pm.MultiMarketplaceMonitor)
        # should return immediately without touching self.email_notifier etc.
        monitor.send_notification([])
