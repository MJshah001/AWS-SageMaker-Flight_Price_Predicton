import numpy as np
import pandas as pd
import streamlit as st
import sklearn
from sklearn.metrics import r2_score
from sklearn.metrics.pairwise import rbf_kernel
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.pipeline import Pipeline, FeatureUnion
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import (
    OneHotEncoder,
    MinMaxScaler,
    StandardScaler,
    PowerTransformer,
    FunctionTransformer,
    OrdinalEncoder
)
from sklearn.ensemble import RandomForestRegressor

from feature_engine.outliers import Winsorizer
from feature_engine.encoding import (
    RareLabelEncoder,
    MeanEncoder,
    CountFrequencyEncoder
)
from feature_engine.datetime import DatetimeFeatures
from feature_engine.selection import SelectBySingleFeaturePerformance

import matplotlib.pyplot as plt

import warnings

import pickle

import xgboost as xgb

import joblib

sklearn.set_config(transform_output="pandas") 
# convinience functions


# preprocessing operations

# airline
airline_transformer = Pipeline(steps=[
    ("imputer",SimpleImputer(strategy="most_frequent")),
    ("grouper",RareLabelEncoder(tol=0.1, replace_with="Other", n_categories=2)),
    ("encoder", OneHotEncoder(sparse_output=False,handle_unknown="ignore"))
])


#doj
features_to_extract = ["month","week","day_of_week","day_of_year"]

doj_transformer = Pipeline(steps= [
    ("dt",DatetimeFeatures(features_to_extract=features_to_extract,yearfirst=True,format="mixed")),
    ("scaler",MinMaxScaler())
])

# source and destination
location_pipe1 = Pipeline(steps=[
    ("grouper",RareLabelEncoder(tol=0.1,replace_with="other",n_categories=2)),
    ("encoder",MeanEncoder()),
    ("scaler",PowerTransformer())
])

def is_north(X):
    columns = X.columns.to_list()
    north_cities = ["Delhi","New Delhi","Kolkata","Mumbai"]
    return(
        X
        .assign(**{
            f"{col}_is_north": X.loc[:, col].isin(north_cities).astype(int)
            for col in columns
        })
        .drop(columns=columns)
    )

location_transformer = FeatureUnion(transformer_list=[
    ("part1",location_pipe1),
    ("part2",FunctionTransformer(func=is_north))
])


#dep_time and arrival_time
time_pipe1 = Pipeline(steps=[
    ("dt",DatetimeFeatures(features_to_extract=["hour","minute"])),
    ("scaler", MinMaxScaler())
])

def part_of_the_day(X,morning=4,afternoon=12,evening=16,night=20):
    columns = X.columns.to_list()
    X_temp = X.assign(**{
        col: pd.to_datetime(X.loc[:,col]).dt.hour
        for col in columns
    })

    return (
        X_temp
        .assign(**{
            f"{col}_part_of_day": np.select(
                [X_temp.loc[:,col].between(morning,afternoon,inclusive="left"),
                X_temp.loc[:,col].between(afternoon,evening,inclusive="left"),
                X_temp.loc[:,col].between(evening,night,inclusive="left"),],
                ["morning","afternoon","evening"],
                default="night"
            )
            for col in columns
        })
        .drop(columns=columns)
    )

time_pipe2 = Pipeline(steps=[
    ("part",FunctionTransformer(func=part_of_the_day)),
    ("encoder",CountFrequencyEncoder()),
    ("scaler", MinMaxScaler())
    
])

time_transformer = FeatureUnion(transformer_list=[
    ("time_part1",time_pipe1),
    ("time_part2",time_pipe2)
])

# Duration
def flight_duration_categories(X,short=180,medium=400):
    return(
        X
        .assign(flight_duration_categories =  np.select([X.duration.lt(short),
                                                        X.duration.between(short,medium,inclusive="left"),],
                                                        ["short","medium"],
                                                        default="long")
               )
        .drop(columns="duration")
    )

def is_over(X,value=1000):
    return (
        X
        .assign(**{
            f"duration_over_{value}": X.duration.ge(value).astype(int)
        })
        .drop(columns="duration")
    )

duration_pipe1 = Pipeline(steps=[
    ("categories",FunctionTransformer(func=flight_duration_categories)),
    ("encoding",OrdinalEncoder(categories=[["short","medium","long"]]))
])

class RBFPercentileSimilarity(BaseEstimator, TransformerMixin):
    def __init__(self, variables=None, percentiles=[0.25,0.5,0.75],gamma=0.1):
        self.variables = variables
        self.percentiles = percentiles
        self.gamma = gamma

    def fit(self,X,y=None):
        if not self.variables:
            self.variables = X.select_dtypes(include="number").columns.to_list()

        self.reference_values_ = {
            col: (
                X
                .loc[:,col]
                .quantile(self.percentiles)
                .values
                .reshape(-1,1)
            )
            for col in self.variables
        }
        
        return self

    def transform(self,X):
        objects = []
        for col in self.variables:
            columns = [f"{col}_rbf_{int(percentile*100)}" for percentile in self.percentiles]
            obj = pd.DataFrame(
                data=rbf_kernel(X.loc[:,[col]],Y=self.reference_values_[col],gamma=self.gamma),
                columns=columns
            )
            objects.append(obj)
        return pd.concat(objects,axis=1)

duration_pipe2 = Pipeline(steps=[
    ("rbf",RBFPercentileSimilarity()),
    ("scaler", PowerTransformer())
])

duration_union = FeatureUnion(transformer_list=[
    ("part1",duration_pipe1),
    ("part2",duration_pipe2),
    ("part3",FunctionTransformer(func=is_over)),
    ("part4", StandardScaler())
])

duration_transformer = Pipeline(steps=[
    ("outliers",Winsorizer(capping_method="iqr",fold=1.5)),
    ("imputer",SimpleImputer(strategy="median")),
    ("union",duration_union)
])

# total_stops
def is_direct(X):
    return (
        X
        .assign(
            is_direct_flight=X.total_stops.eq(0).astype(int)
        )
    )

total_stops_transformer = Pipeline(steps=[
    ("imputer",SimpleImputer(strategy="most_frequent")),
    ("direct",FunctionTransformer(func=is_direct))
])

additional_info_pipe1 = Pipeline(steps=[
    ("group",RareLabelEncoder(tol=0.1,n_categories=2,replace_with="other")),
    ("encoder",OneHotEncoder(handle_unknown="ignore",sparse_output=False))
])

def have_info(X):
    return X.assign(additional_info=X.additional_info.ne("No Info").astype(int))

info_union = FeatureUnion(transformer_list=[
    ("part1",additional_info_pipe1),
    ("part2",FunctionTransformer(func=have_info))
])

additional_info_transformer = Pipeline(steps=[
    ("imputer",SimpleImputer(strategy="constant",fill_value="unknown")),
    ("union",info_union)
])


# column transformer
column_transformer = ColumnTransformer(transformers=[
    ("airline",airline_transformer,["airline"]),
    ("doj",doj_transformer,["date_of_journey"]),
    ("location",location_transformer,["source","destination"]),
    ("time",time_transformer,["dep_time","arrival_time"]),
    ("dur",duration_transformer,["duration"]),
    ("stops",total_stops_transformer,["total_stops"]),
    ("info",additional_info_transformer,["additional_info"])
],remainder="passthrough")


# feature selector
estimator = RandomForestRegressor(n_estimators=10,max_depth=3,random_state=9)

selector = SelectBySingleFeaturePerformance(
    estimator=estimator,
    scoring="r2",
    threshold=0.1
)

# final preprocessor pipeline
preprocessor = Pipeline(steps=[
    ("column_tranformer",column_transformer),
    ("selector",selector)
])

# read the data
path = "train.csv"
train = pd.read_csv(path)
X_train = train.drop(columns="price")
y_train = train.price.copy()

# fit and save the preprocessor
preprocessor.fit(X_train,y_train)
joblib.dump(preprocessor,"preprocessor.joblib")


# web application

st.set_page_config(
    page_title="Flight Price Prediction",
    page_icon="✈️",
    layout="wide"
)

st.title("Flight Price Prediction")

# user inputs

airline = st.selectbox(
    "Airline:",
    options=X_train.airline.unique()
)

doj = st.date_input("Date of Journey:")

source = st.selectbox(
    "Source:",
    options=X_train.source.unique()
)

destination = st.selectbox(
    "Destination:",
    options=X_train.destination.unique()
)

dep_time = st.time_input("Departure Time:")

arrival_time = st.time_input("Arrival Time:")

duration = st.number_input(
    "Duration:",
    step=5,
    min_value=0
)

total_stops = st.number_input(
    "Total Stops:",
    step=1,
    min_value=0
)

additional_info = st.selectbox(
    "Additional Info:",
    options=X_train.additional_info.unique()
)

x_new = pd.DataFrame(dict(
    airline=[airline],
    date_of_journey=[doj],
    source=[source],
    destination=[destination],
    dep_time=[dep_time],
    arrival_time=[arrival_time],
    duration=[duration],
    total_stops=[total_stops],
    additional_info=[additional_info]
)).astype({
    col: "str"
    for col in ["date_of_journey","dep_time","arrival_time"]
})

if st.button("Predict Price"):
    saved_preprocessor = joblib.load("preprocessor.joblib")
    x_new_preprocessed = saved_preprocessor.transform(x_new)

    with open("xgboost-model","rb") as f:
        model = pickle.load(f)
    x_new_xgb = xgb.DMatrix(x_new_preprocessed)
    pred = model.predict(x_new_xgb)[0]
    # pred = 10000 # testing

    st.info(f"The predicted price is {pred:.2f} INR")