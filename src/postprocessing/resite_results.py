import sys
from os import listdir
import pickle

import pandas as pd

from src.resite.resite import Resite


class ResiteResults:

    def __init__(self, resite: Resite):
        self.resite = resite
        self.existing_nodes = self.resite.existing_capacity_ds[self.resite.existing_capacity_ds > 0].index

    def print_summary(self):
        print(f"\nRegion: {self.resite.regions}")
        print(f"Technologies: {self.resite.technologies}")
        print(f"Formulation: {self.resite.formulation}")
        print(f"Deployement vector: {self.resite.deployment_vector}\n")

    def print_number_of_points(self):
        count = pd.DataFrame(0., index=sorted(list(self.resite.tech_points_dict.keys())),
                             columns=["Initial", "Selected", "With existing cap"], dtype=int)
        for tech, points in self.resite.tech_points_dict.items():
            count.loc[tech, "Initial"] = int(len(points))
        for tech, points in self.resite.selected_tech_points_dict.items():
            count.loc[tech, "Selected"] = int(len(points))
        for tech, point in self.resite.existing_capacity_ds.index:
            count.loc[tech, "With existing cap"] += 1 if self.resite.existing_capacity_ds.loc[tech, point] > 0 else 0
        print(f"Number of points:\n{count}\n")

    def get_initial_capacity_potential(self):
        return self.resite.cap_potential_ds.groupby(level=0).sum()

    def get_selected_capacity_potential(self):
        return self.resite.selected_capacity_potential_ds.groupby(level=0).sum()

    def print_capacity_potential(self):
        initial_cap_potential = self.get_initial_capacity_potential()
        selected_cap_potential = self.get_selected_capacity_potential()
        cap_potential = pd.concat([initial_cap_potential, selected_cap_potential], axis=1, sort=True)
        cap_potential.columns = ["Initial", "Selected", "%"]
        print(f"Capacity potential (GW):\n{cap_potential}\n")

    def get_existing_capacity(self):
        return self.resite.existing_capacity_ds.groupby(level=0).sum()

    def get_optimal_capacity(self):
        return self.resite.optimal_capacity_ds.groupby(level=0).sum()

    def get_optimal_capacity_at_existing_nodes(self):
        return self.resite.optimal_capacity_ds[self.existing_nodes].groupby(level=0).sum()

    def print_capacity(self):
        existing_cap = self.get_existing_capacity()
        optimal_cap = self.get_optimal_capacity()
        optimal_cap_at_ex_nodes = self.get_optimal_capacity_at_existing_nodes()
        capacities = pd.concat([existing_cap, optimal_cap, optimal_cap_at_ex_nodes], axis=1, sort=True)
        capacities.columns = ["Existing", "Optimal", "Optimal at existing nodes"]
        print(f"Capacity (GW):\n{capacities}\n")

    def print_generation(self):
        generation = self.resite.optimal_capacity_ds*self.resite.cap_factor_df
        generation_per_type = pd.DataFrame(generation.sum().groupby(level=0).sum(), columns=["GWh"])
        generation_per_type["% of Total"] = generation_per_type["GWh"]/generation_per_type["GWh"].sum()
        generation_per_type["At Existing Nodes"] = generation[self.existing_nodes].sum().groupby(level=0).sum()
        print(f"Generation (GWh):\n{generation_per_type}\n")

    def get_initial_cap_factor_mean(self):
        return self.resite.cap_factor_df.mean().groupby(level=0).mean()

    def get_selected_cap_factor_mean(self):
        return self.resite.selected_cap_factor_df.mean().groupby(level=0).mean()

    def print_cap_factor_mean(self):
        initial_cap_factor_mean = self.get_initial_cap_factor_mean()
        selected_cap_fator_mean = self.get_selected_cap_factor_mean()
        cap_factor_mean = pd.concat([initial_cap_factor_mean, selected_cap_fator_mean], axis=1, sort=True)
        cap_factor_mean.columns = ["Initial", "Selected"]
        print(f"Mean of mean of capacity factors:\n{cap_factor_mean}\n")

    def get_initial_cap_factor_std(self):
        return self.resite.cap_factor_df.std().groupby(level=0).mean()

    def get_selected_cap_factor_std(self):
        return self.resite.selected_cap_factor_df.std().groupby(level=0).mean()

    def print_cap_factor_std(self):
        initial_cap_factor_std = self.get_initial_cap_factor_std()
        selected_cap_fator_std = self.get_selected_cap_factor_std()
        cap_factor_std = pd.concat([initial_cap_factor_std, selected_cap_fator_std], axis=1, sort=True)
        cap_factor_std.columns = ["Initial", "Selected"]
        print(f"Mean of std of capacity factors:\n{cap_factor_std}\n")


if __name__ == "__main__":

    assert (len(sys.argv) == 2) or (len(sys.argv) == 3), \
        "You need to provide one or two argument: output_dir (and test_number)"

    main_output_dir = sys.argv[1]
    test_number = sys.argv[2] if len(sys.argv) == 3 else None
    if test_number is None:
        test_number = sorted(listdir(main_output_dir))[-1]
    output_dir = main_output_dir + test_number + "/"
    print(output_dir)

    resite = pickle.load(open(output_dir + "resite_model.p", 'rb'))

    ro = ResiteResults(resite)
    ro.print_summary()

    ro.print_number_of_points()

    ro.print_capacity()
    ro.print_capacity_potential()

    ro.print_cap_factor_mean()
    ro.print_cap_factor_std()

    ro.print_generation()
