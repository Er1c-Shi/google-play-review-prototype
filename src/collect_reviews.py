import os
import time
import pandas as pd
from google_play_scraper import reviews, Sort


APPS = {
    "Spotify": "com.spotify.music",
    "Duolingo": "com.duolingo",
    "Instagram": "com.instagram.android",
    "TikTok": "com.zhiliaoapp.musically",
    "WhatsApp": "com.whatsapp",
}

REVIEWS_PER_APP = 2000
OUTPUT_PATH = "data/raw/google_play_reviews_sample.csv"


def collect_reviews_for_app(app_name, app_id, n_reviews):
    all_reviews = []
    continuation_token = None

    while len(all_reviews) < n_reviews:
        batch_size = min(200, n_reviews - len(all_reviews))

        batch, continuation_token = reviews(
            app_id,
            lang="en",
            country="us",
            sort=Sort.NEWEST,
            count=batch_size,
            continuation_token=continuation_token,
        )

        if not batch:
            print(f"No more reviews returned for {app_name}.")
            break

        for review in batch:
            review["app_name"] = app_name
            review["app_id"] = app_id

        all_reviews.extend(batch)

        print(f"{app_name}: collected {len(all_reviews)} reviews")

        if continuation_token is None:
            break

        time.sleep(1)

    return all_reviews


def main():
    os.makedirs("data/raw", exist_ok=True)

    all_rows = []

    for app_name, app_id in APPS.items():
        print(f"\nCollecting reviews for {app_name}...")
        app_reviews = collect_reviews_for_app(
            app_name=app_name,
            app_id=app_id,
            n_reviews=REVIEWS_PER_APP,
        )
        all_rows.extend(app_reviews)

    df = pd.DataFrame(all_rows)
    df.to_csv(OUTPUT_PATH, index=False)

    print("\nCollection finished.")
    print(f"Total reviews collected: {len(df)}")
    print(f"Saved to: {OUTPUT_PATH}")
    print("\nColumns:")
    print(df.columns.tolist())
    print("\nFirst few rows:")
    print(df.head())


if __name__ == "__main__":
    main()