import time
from pyspark.sql import SparkSession, functions as F, Window
from pyspark.sql.functions import broadcast

# =========================
# CONFIG
# =========================
GCS_PATH = "gs://tbd-phase2/social_media_data_by_category"
OUT_GCS_DIR = "gs://tbd-phase2/results_2/task2"
WORKERS_TAG = "w3"  # set to: w2 / w3 / w4

WARMUP = 2
REPEATS = 5

COL_POST = "post_id"
COL_CAT = "category"
COL_LOC = "location"
COL_LIKES = "likes"
COL_VIEWS = "views"

# =========================
# Spark
# =========================
spark = SparkSession.builder.appName(f"phase2-task2-{WORKERS_TAG}").getOrCreate()

workers = int(WORKERS_TAG[1:]) if WORKERS_TAG.startswith("w") else 2
target_parts = max(24, 12 * workers)

spark.conf.set("spark.sql.adaptive.enabled", "true")
spark.conf.set("spark.sql.adaptive.coalescePartitions.enabled", "true")
spark.conf.set("spark.sql.shuffle.partitions", str(target_parts))
spark.conf.set("spark.default.parallelism", str(target_parts))
spark.conf.set("spark.sql.broadcastTimeout", "600")

def timed(action_fn):
    for _ in range(WARMUP):
        action_fn()
    times = []
    last = None
    for _ in range(REPEATS):
        t0 = time.time()
        last = action_fn()
        t1 = time.time()
        times.append(t1 - t0)
    return float(min(times)), float(sum(times) / len(times)), int(last)

df = spark.read.parquet(GCS_PATH)

# Align partitions with main grouping key to reduce shuffle on window/join-heavy queries
df = df.repartition(target_parts, COL_CAT).persist()
_ = df.count()

def qA(sdf):
    return (sdf.where(F.col(COL_VIEWS) >= 100)
              .groupBy(COL_CAT, COL_LOC)
              .agg(F.avg(COL_LIKES).alias("avg_likes"),
                   F.sum(COL_VIEWS).alias("sum_views"),
                   F.count(F.lit(1)).alias("posts"))
              .orderBy(F.col("sum_views").desc()))

def qB(sdf):
    w = Window.partitionBy(COL_CAT).orderBy(F.col(COL_LIKES).desc())
    return (sdf.select(COL_CAT, COL_POST, COL_LIKES)
              .withColumn("rk", F.dense_rank().over(w))
              .where(F.col("rk") <= 3)
              .orderBy(COL_CAT, "rk"))

def qC(sdf):
    dim = (sdf.select(COL_CAT).distinct().limit(50)
             .withColumn("weight", (F.row_number().over(Window.orderBy(COL_CAT)) % 7 + 1).cast("double")))
    j = (sdf.join(broadcast(dim), on=COL_CAT, how="inner")
           .withColumn("wlikes", F.col(COL_LIKES) * F.col("weight")))
    return (j.groupBy(COL_CAT)
              .agg(F.sum("wlikes").alias("weighted_likes"),
                   F.count(F.lit(1)).alias("posts"))
              .orderBy(F.col("weighted_likes").desc()))

def run(qid, build):
    def _do():
        return build(df).count()
    tmin, tmean, rows = timed(_do)
    return (
        WORKERS_TAG,
        workers,
        qid,
        tmin,
        tmean,
        rows,
        target_parts,
        spark.sparkContext.master,
        spark.sparkContext.applicationId,
        spark.conf.get("spark.sql.adaptive.enabled"),
        spark.conf.get("spark.sql.shuffle.partitions"),
    )

rows = []
for qid, fn in [("A", qA), ("B", qB), ("C", qC)]:
    rows.append(run(qid, fn))

schema = """
run_tag string,
workers int,
query string,
time_min_s double,
time_mean_s double,
rows_out long,
partitions int,
master string,
app_id string,
aqe_enabled string,
shuffle_partitions string
"""

out_path = f"{OUT_GCS_DIR}/phase2_task2_{WORKERS_TAG}"
spark.createDataFrame(rows, schema=schema) \
     .coalesce(1) \
     .write \
     .mode("overwrite") \
     .option("header", True) \
     .csv(out_path)

print("Saved:", out_path)
df.unpersist()
spark.stop()
