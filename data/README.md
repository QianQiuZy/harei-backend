# Music seed CSVs

`songs.csv` and `song_performances.csv` are one-time UTF-8/LF snapshots generated from the live dataset: 478 songs and 2,685 performances. Regenerate with `python scripts/generate_music_csv.py` before a manual import.

```sql
CREATE TEMPORARY TABLE stage_songs (source_key VARCHAR(80) NOT NULL,title VARCHAR(255) NOT NULL,artist VARCHAR(500) NOT NULL,artists_json TEXT NOT NULL,genre VARCHAR(100) NOT NULL,language VARCHAR(50) NOT NULL,work_type VARCHAR(50) NOT NULL,notes TEXT NOT NULL,metadata_status VARCHAR(30) NOT NULL,status VARCHAR(20) NOT NULL);
LOAD DATA LOCAL INFILE '/absolute/path/data/songs.csv' INTO TABLE stage_songs CHARACTER SET utf8mb4 FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '"' LINES TERMINATED BY '\n' IGNORE 1 LINES (source_key,title,artist,artists_json,genre,language,work_type,notes,metadata_status,status);
INSERT INTO songs (source_key,title,artist,artists,genre,language,work_type,notes,metadata_status,status,version) SELECT source_key,title,artist,CAST(artists_json AS JSON),genre,language,work_type,notes,metadata_status,status,1 FROM stage_songs;
CREATE TEMPORARY TABLE stage_performances (source_key VARCHAR(80) NOT NULL,song_source_key VARCHAR(80) NOT NULL,performed_on DATE NOT NULL,platform VARCHAR(100) NOT NULL,stream_id VARCHAR(100),stream_title VARCHAR(255),stream_url VARCHAR(2048),clip_url VARCHAR(2048));
LOAD DATA LOCAL INFILE '/absolute/path/data/song_performances.csv' INTO TABLE stage_performances CHARACTER SET utf8mb4 FIELDS TERMINATED BY ',' ENCLOSED BY '"' ESCAPED BY '"' LINES TERMINATED BY '\n' IGNORE 1 LINES;
INSERT INTO song_performances (source_key,song_id,performed_on,platform,stream_id,stream_title,stream_url,clip_url) SELECT p.source_key,s.song_id,p.performed_on,p.platform,NULLIF(p.stream_id,''),NULLIF(p.stream_title,''),NULLIF(p.stream_url,''),NULLIF(p.clip_url,'') FROM stage_performances p JOIN songs s ON s.source_key=p.song_source_key;
UPDATE music_catalog_revision SET revision = revision + 1 WHERE id = 1;
SELECT COUNT(*) AS songs_loaded FROM songs; SELECT COUNT(*) AS performances_loaded FROM song_performances; SELECT COUNT(*) AS orphans FROM stage_performances p LEFT JOIN songs s ON s.source_key=p.song_source_key WHERE s.song_id IS NULL; SELECT revision FROM music_catalog_revision WHERE id = 1;
```

Expected validations are `478`, `2685`, `0`, and revision `1`. Incrementing the revision after both inserts invalidates any empty-catalog ETag cached during deployment. The API intentionally has no import endpoint.
