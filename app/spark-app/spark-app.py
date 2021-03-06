from pyspark.sql import SparkSession
from pyspark.sql.functions import *
from pyspark.sql.types import IntegerType, StringType, StructType, TimestampType
import mysqlx

dbOptions = {"host": "my-app-mysql-service", 'port': 33060, "user": "root", "password": "mysecretpw"}
dbSchema = 'popular'
windowDuration = '5 minutes'
slidingDuration = '1 minute'

# Create a spark session
spark = SparkSession.builder \
    .appName("Structured Streaming").getOrCreate()

# Set log level
spark.sparkContext.setLogLevel('WARN')

# Read messages from Kafka
kafkaMessages = spark \
    .readStream \
    .format("kafka") \
    .option("kafka.bootstrap.servers",
            "my-cluster-kafka-bootstrap:9092") \
    .option("subscribe", "tracking-data") \
    .option("startingOffsets", "earliest") \
    .load()

# Define schema of tracking data
trackingMessageSchema = StructType() \
    .add("id", StringType()) \
    .add("author", StringType()) \
    .add("timestamp", IntegerType())


# Convert value: binary -> JSON -> fields + parsed timestamp
trackingMessages = kafkaMessages.select(
    # Extract 'value' from Kafka message (i.e., the tracking data)
    from_json(
        column("value").cast("string"),
        trackingMessageSchema
    ).alias("json")
).select(
    # Convert Unix timestamp to TimestampType
    from_unixtime(column('json.timestamp'))
    .cast(TimestampType())
    .alias("parsed_timestamp"),

    # Select all JSON fields
    column("json.*")
) \
    .withColumnRenamed('json.id', 'id') \
    .withColumnRenamed('json.author', 'author') \
    .withWatermark("parsed_timestamp", windowDuration)

# Compute most popular books
popular = trackingMessages.groupBy(
    window(
        column("parsed_timestamp"),
        windowDuration,
        slidingDuration
    ),
    column("id")
).count().withColumnRenamed('count', 'views')

# Compute most popular authors
popularAuthors = trackingMessages.groupBy(
        window(
            column("parsed_timestamp"),
            windowDuration,
            slidingDuration
        ),
        column("author")
    ).count().withColumnRenamed('count', 'views')

# Start running the query for books; print running counts to the console
consoleDump = popular \
    .writeStream \
    .trigger(processingTime=slidingDuration) \
    .outputMode("update") \
    .format("console") \
    .option("truncate", "false") \
    .start()

# Start running the query for authors; print running counts to the console
consoleDump2 = popularAuthors \
    .writeStream \
    .trigger(processingTime=slidingDuration) \
    .outputMode("update") \
    .format("console") \
    .option("truncate", "false") \
    .start()


def saveToDatabase(batchDataframe, batchId):
    # Define function to save a dataframe to mysql
    def save_to_db(iterator):
        # Connect to database and use schema
        session = mysqlx.get_session(dbOptions)
        session.sql("USE popular").execute()

        for row in iterator:
            # Run upsert (insert or update existing)
            sql = session.sql("INSERT INTO popular "
                              "(book, count) VALUES (?, ?) "
                              "ON DUPLICATE KEY UPDATE count=?")
            sql.bind(row.id, row.views, row.views).execute()
            print("Wrote popular books to database: ",row.id)
        session.close()
    # Perform batch UPSERTS per data partition
    batchDataframe.foreachPartition(save_to_db)

def saveAuthorsToDatabase(batchDataframe, batchId):
    # Define function to save a dataframe to mysql
    def save_to_db(iterator):
        # Connect to database and use schema
        session = mysqlx.get_session(dbOptions)
        session.sql("USE popular").execute()
        for row in iterator:
            # Run upsert (insert or update existing)
            sql = session.sql("INSERT INTO popularAuthors "
                              "(author, count) VALUES (?, ?) "
                              "ON DUPLICATE KEY UPDATE count=?")
            sql.bind(row.author, row.views, row.views).execute()
            print("Wrote popular authors to database: ",row.author)
        session.close()

    # Perform batch UPSERTS per data partition
    batchDataframe.foreachPartition(save_to_db)


dbInsertStream = popular.writeStream \
    .trigger(processingTime=slidingDuration) \
    .outputMode("update") \
    .foreachBatch(saveToDatabase) \
    .start()

dbInsertStream2 = popularAuthors.writeStream \
    .trigger(processingTime=slidingDuration) \
    .outputMode("update") \
    .foreachBatch(saveAuthorsToDatabase) \
    .start()


# Wait for termination
spark.streams.awaitAnyTermination()
